"""OneDrive folder sync: Graph client, sign-in token, isolated collection,
change detection, reconcile, and the retriever's OneDrive block."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from openexecutive.config import Settings
from openexecutive.knowledge import onedrive_account, onedrive_sync
from openexecutive.knowledge.onedrive_client import (
    OneDriveClient,
    OneDriveDownloadFailed,
    item_key,
    parse_folder_entry,
    parse_item_key,
    share_id,
)
from openexecutive.knowledge.store import ChromaDBStore

DRIVE = "b!AbC-12_x"
OTHER_DRIVE = "d4648f06c91d9d3d"
ROOT = "01ROOTFOLDER"
SUB = "01SUBFOLDER"
DOCX = "01DOCXFILE"
TXT = "01TEXTFILE"
PPTX = "01DECKFILE"
IMG = "01IMAGEFILE"
REMOTE = "D4648F06C91D9D3D!54927"
PLAN = "01PLANFILE"
COLLECTION = ChromaDBStore.ONEDRIVE_COLLECTION
DOWNLOAD_HOST = "public.dm.files.1drv.com"


class FakeStore:
    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, Any]]] = {}

    def add_documents(self, texts, metadatas, ids, collection):
        col = self.collections.setdefault(collection, [])
        for t, m, i in zip(texts, metadatas, ids, strict=False):
            col[:] = [r for r in col if r["id"] != i]
            col.append({"id": i, "text": t, "metadata": m})

    def delete_documents(self, collection, where):
        col = self.collections.get(collection, [])
        self.collections[collection] = [
            r for r in col if not all(r["metadata"].get(k) == v for k, v in where.items())
        ]

    def query(self, query_text, collection, domain_filter=None, n_results=5):
        return [
            {"text": r["text"], "metadata": r["metadata"], "distance": 0.1}
            for r in self.collections.get(collection, [])[:n_results]
        ]

    def delete_onedrive_docs(self) -> None:
        self.delete_documents(COLLECTION, {"type": "onedrive"})

    def synced_keys(self) -> set[str]:
        return {r["metadata"]["onedrive_key"] for r in self.collections.get(COLLECTION, [])}


def _file(item_id: str, name: str, *, mime: str = "application/octet-stream",
          ctag: str = "c1", modified: str = "2026-09-01T00:00:00Z",
          size: int | None = 10) -> dict[str, Any]:
    return {
        "id": item_id, "name": name, "file": {"mimeType": mime}, "cTag": ctag,
        "lastModifiedDateTime": modified, "size": size,
        "webUrl": f"https://contoso-my.sharepoint.com/{item_id}",
    }


def _folder(item_id: str, name: str) -> dict[str, Any]:
    return {"id": item_id, "name": name, "folder": {"childCount": 1}}


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class FakeGraph:
    """Microsoft Graph's drive endpoints and the download host behind an
    httpx.MockTransport. ``/content`` redirects to the download host, as
    Graph does, and the download host refuses a bearer token."""

    def __init__(self) -> None:
        self.folders: dict[str, list[dict[str, Any]]] = {
            ROOT: [
                _file(DOCX, "Q3 Finance Plan.docx", modified="2026-09-03T00:00:00Z"),
                _file(PPTX, "Board deck.pptx", modified="2026-09-02T00:00:00Z"),
                _file(IMG, "Logo.png"),
                _folder(SUB, "Contracts"),
            ],
            SUB: [
                _file(TXT, "notes.txt"),
                _file(PLAN, "Plan.md", mime="text/markdown"),
                # A shortcut to a folder or file elsewhere: never followed.
                {
                    "id": "local-shortcut", "name": "Shared plan.md",
                    "remoteItem": {
                        "id": REMOTE, "file": {"mimeType": "text/markdown"},
                        "parentReference": {"driveId": OTHER_DRIVE}, "cTag": "r1",
                        "lastModifiedDateTime": "2026-09-01T00:00:00Z", "size": 5,
                        "webUrl": "https://onedrive.live.com/x",
                    },
                },
            ],
        }
        self.content: dict[str, bytes] = {
            DOCX: _docx_bytes("Revenue grows 12% in Q3."),
            PPTX: b"%PDF-deck",
            TXT: b"Vendor terms: net 30.",
            PLAN: b"Hiring plan: two engineers.",
            REMOTE: b"Board minutes: confidential.",
        }
        self.page_size = 100
        self.fail_list: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.next_link_host = "graph.microsoft.com"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == DOWNLOAD_HOST:
            assert "Authorization" not in request.headers
            item = request.url.path.strip("/")
            data = self.content.get(item)
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"] == "Bearer tok"
        parts = request.url.path.split("/")  # /v1.0/drives/{d}/items/{i}/...
        item = parts[5]
        if request.url.path.endswith("/children"):
            if item in self.fail_list:
                return httpx.Response(403, json={"error": {"code": "accessDenied"}})
            items = self.folders.get(item, [])
            start = int(request.url.params.get("skip") or 0)
            page = items[start:start + self.page_size]
            body: dict[str, Any] = {"value": page}
            if start + self.page_size < len(items):
                body["@odata.nextLink"] = (
                    f"https://{self.next_link_host}/v1.0/drives/{parts[3]}/items/{item}"
                    f"/children?skip={start + self.page_size}"
                )
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/content"):
            if item not in self.content:
                return httpx.Response(404)
            suffix = "?format=pdf" if request.url.params.get("format") == "pdf" else ""
            return httpx.Response(
                302, headers={"Location": f"https://{DOWNLOAD_HOST}/{item}{suffix}"}
            )
        return httpx.Response(404)

    def client(self) -> OneDriveClient:
        async def token() -> str:
            return "tok"

        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return OneDriveClient(http, token)


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    monkeypatch.setenv("ONEDRIVE_SYNC_ENABLED", "true")
    monkeypatch.setenv("ONEDRIVE_SYNC_FOLDERS", f"{DRIVE}/{ROOT}")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "profile.yaml"))
    monkeypatch.setattr(onedrive_sync, "_sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _fresh_fixture_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.cli import fixture_loader

    monkeypatch.setattr(fixture_loader, "_FIXTURE_OP_LOCK", asyncio.Lock())


@pytest.fixture(autouse=True)
def _pdf_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Graph's PDF rendering is faked as bytes, so read it as text here."""
    real = onedrive_sync._extract

    def extract(data: bytes, suffix: str) -> str:
        if suffix == ".pdf":
            return "Deck slide: expand to Europe." if data == b"%PDF-deck" else ""
        return real(data, suffix)

    monkeypatch.setattr(onedrive_sync, "_extract", extract)


async def _sync(graph: FakeGraph, store: FakeStore, **kw: Any) -> dict[str, int]:
    return await onedrive_sync.run_onedrive_sync(store=store, client=graph.client(), **kw)  # type: ignore[arg-type]


def _state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "onedrive_sync_state.json").read_text())


# --- client helpers ---------------------------------------------------------


def test_folder_entries_and_keys_round_trip() -> None:
    assert parse_folder_entry(f" {DRIVE}/{ROOT} ") == (DRIVE, ROOT)
    for bad in ("", ROOT, f"{DRIVE}/{ROOT}/x", f"{DRIVE}/../etc", "a b/c", f"{DRIVE}/{ROOT}?x=1"):
        assert parse_folder_entry(bad) is None
    key = item_key(OTHER_DRIVE, REMOTE)
    assert parse_item_key(key) == (OTHER_DRIVE, REMOTE)
    assert parse_item_key(f"{key}:x") is None and parse_item_key("a/b:c") is None


def test_share_links_are_encoded_only_for_microsoft_hosts() -> None:
    sid = share_id("https://1drv.ms/f/s!AbcDef")
    assert sid.startswith("u!") and "=" not in sid and "/" not in sid
    assert share_id("https://contoso.sharepoint.com/:f:/g/abc").startswith("u!")
    for bad in (
        "http://1drv.ms/f/s!x",
        "https://evil.example/1drv.ms",
        "https://1drv.ms.evil.example/x",
        "https://user@1drv.ms/x",
        "https://1drv.ms:8443/x",
    ):
        with pytest.raises(ValueError):
            share_id(bad)


def test_settings_validate_folder_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDRIVE_SYNC_FOLDERS", f"{DRIVE}/{ROOT}, ,{DRIVE}/{ROOT},{OTHER_DRIVE}/{SUB}")
    assert Settings().onedrive_sync_folder_list == [(DRIVE, ROOT), (OTHER_DRIVE, SUB)]  # type: ignore[call-arg]
    monkeypatch.setenv("ONEDRIVE_SYNC_FOLDERS", "https://1drv.ms/f/s!x")
    with pytest.raises(ValueError, match="drive id"):
        Settings()  # type: ignore[call-arg]
    monkeypatch.setenv("ONEDRIVE_SYNC_FOLDERS", "")
    with pytest.raises(ValueError, match="requires ONEDRIVE_SYNC_FOLDERS"):
        Settings()  # type: ignore[call-arg]
    monkeypatch.setenv("ONEDRIVE_SYNC_ENABLED", "false")
    assert Settings().onedrive_sync_folder_list == []  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_a_paging_link_off_graph_is_not_followed() -> None:
    graph = FakeGraph()
    graph.page_size = 1
    graph.next_link_host = "evil.example"
    items, truncated = await graph.client().list_folder(DRIVE, ROOT, max_items=50)
    assert len(items) == 1 and truncated
    assert all(r.url.host == "graph.microsoft.com" for r in graph.requests)


@pytest.mark.asyncio
async def test_a_failed_download_never_names_its_url() -> None:
    graph = FakeGraph()
    graph.content[DOCX] = b"x"
    client = graph.client()

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.host == DOWNLOAD_HOST:
            return httpx.Response(403)
        return graph.handler(request)

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(OneDriveDownloadFailed) as caught:
        await client.download(DRIVE, DOCX, max_bytes=100)
    assert DOWNLOAD_HOST not in str(caught.value) and "403" in str(caught.value)


@pytest.mark.asyncio
async def test_unsafe_ids_never_reach_a_url() -> None:
    graph = FakeGraph()
    with pytest.raises(ValueError):
        await graph.client().list_folder(DRIVE, "../me", max_items=5)
    with pytest.raises(ValueError):
        await graph.client().download("a/b", DOCX, max_bytes=5)
    assert not graph.requests


# --- sync ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_sync_indexes_supported_files_into_the_onedrive_collection(tmp_path: Path) -> None:
    graph, store = FakeGraph(), FakeStore()
    stats = await _sync(graph, store)
    assert stats["seen"] == 5 and stats["updated"] == 4 and stats["skipped"] == 1  # the image
    assert store.synced_keys() == {
        item_key(DRIVE, DOCX), item_key(DRIVE, PPTX), item_key(DRIVE, TXT), item_key(DRIVE, PLAN),
    }
    assert ChromaDBStore.COMPANY_COLLECTION not in store.collections
    texts = " ".join(r["text"] for r in store.collections[COLLECTION])
    for expected in ("Revenue grows 12%", "expand to Europe", "net 30", "two engineers"):
        assert expected in texts
    # The shortcut in Contracts points outside the listed folders, so its
    # target is never read.
    assert "Board minutes" not in texts
    assert not any(REMOTE in str(r.url) for r in graph.requests)
    meta = next(
        r["metadata"] for r in store.collections[COLLECTION]
        if r["metadata"]["onedrive_key"] == item_key(DRIVE, DOCX)
    )
    assert meta["type"] == "onedrive" and meta["name"] == "Q3 Finance Plan.docx"
    assert meta["url"].startswith("https://") and meta["synced_at"] and meta["domain"] == "finance"
    # The deck went through Graph's PDF conversion; the rest were read as stored.
    pdf = [r for r in graph.requests if r.url.params.get("format") == "pdf"]
    assert [r.url.path.split("/")[5] for r in pdf if r.url.host == "graph.microsoft.com"] == [PPTX]
    files = sorted(p.name for p in (tmp_path / "docs" / "onedrive").iterdir())
    assert len(files) == 4 and all(f.startswith("onedrive-") for f in files)


@pytest.mark.asyncio
async def test_unchanged_files_are_skipped_and_changed_ones_reindexed() -> None:
    graph, store = FakeGraph(), FakeStore()
    await _sync(graph, store)
    graph.requests.clear()
    stats = await _sync(graph, store)
    assert stats["updated"] == 0 and stats["skipped"] == 5
    assert not [r for r in graph.requests if r.url.path.endswith("/content")]

    graph.folders[SUB][0] = _file(TXT, "notes.txt", ctag="c2")
    graph.content[TXT] = b"Vendor terms: net 45."
    stats = await _sync(graph, store)
    assert stats["updated"] == 1
    texts = " ".join(r["text"] for r in store.collections[COLLECTION])
    assert "net 45" in texts and "net 30" not in texts


@pytest.mark.asyncio
async def test_removed_file_is_purged(tmp_path: Path) -> None:
    graph, store = FakeGraph(), FakeStore()
    await _sync(graph, store)
    graph.folders[SUB] = graph.folders[SUB][1:]
    stats = await _sync(graph, store)
    assert stats["purged"] == 1
    assert item_key(DRIVE, TXT) not in store.synced_keys()
    assert item_key(DRIVE, TXT) not in _state(tmp_path)["files"]
    assert len(list((tmp_path / "docs" / "onedrive").iterdir())) == 3


@pytest.mark.asyncio
async def test_a_folder_that_fails_to_list_skips_reconcile(tmp_path: Path) -> None:
    graph, store = FakeGraph(), FakeStore()
    await _sync(graph, store)
    graph.fail_list.add(SUB)
    stats = await _sync(graph, store)
    assert stats["purged"] == 0 and item_key(DRIVE, TXT) in store.synced_keys()
    assert _state(tmp_path)["reconcile_skips"] == 1


@pytest.mark.asyncio
async def test_a_blank_listing_does_not_mass_purge() -> None:
    graph, store = FakeGraph(), FakeStore()
    await _sync(graph, store)
    graph.folders = {}
    stats = await _sync(graph, store)
    assert stats["purged"] == 0 and len(store.synced_keys()) == 4


@pytest.mark.asyncio
async def test_oversize_files_are_recorded_without_chunks_and_not_refetched(tmp_path: Path) -> None:
    graph = FakeGraph()
    graph.folders[SUB][0] = _file(TXT, "notes.txt", size=onedrive_sync._MAX_FILE_BYTES + 1)
    store = FakeStore()
    await _sync(graph, store)
    assert item_key(DRIVE, TXT) not in store.synced_keys()
    assert _state(tmp_path)["files"][item_key(DRIVE, TXT)]["filename"] == ""
    graph.requests.clear()
    await _sync(graph, store)
    assert not [r for r in graph.requests if r.url.path.endswith("/content")]


@pytest.mark.asyncio
async def test_a_fetch_error_is_retried_next_tick(tmp_path: Path) -> None:
    graph = FakeGraph()
    del graph.content[DOCX]
    store = FakeStore()
    stats = await _sync(graph, store)
    assert stats["failed"] == 1 and item_key(DRIVE, DOCX) not in store.synced_keys()
    assert "could not be synced" in _state(tmp_path)["last_error"]
    graph.content[DOCX] = _docx_bytes("now readable")
    await _sync(graph, store)
    assert item_key(DRIVE, DOCX) in store.synced_keys() and "last_error" not in _state(tmp_path)


@pytest.mark.asyncio
async def test_per_scan_cap_takes_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDRIVE_MAX_FILES_PER_SCAN", "1")
    store = FakeStore()
    stats = await _sync(FakeGraph(), store)
    assert stats["capped"] == 3 and store.synced_keys() == {item_key(DRIVE, DOCX)}


@pytest.mark.asyncio
async def test_disabled_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDRIVE_SYNC_ENABLED", "false")
    graph = FakeGraph()
    assert (await _sync(graph, FakeStore()))["seen"] == 0
    assert not graph.requests


@pytest.mark.asyncio
async def test_a_refused_sign_in_stops_the_tick_with_a_reason(tmp_path: Path) -> None:
    graph = FakeGraph()
    client = graph.client()

    async def refused() -> str:
        raise onedrive_account.OneDriveCredentialMissing("Microsoft refused the saved sign-in")

    client._token = refused
    stats = await onedrive_sync.run_onedrive_sync(store=FakeStore(), client=client)  # type: ignore[arg-type]
    assert stats["failed"] == 1 and not graph.requests
    assert "Sign in to Microsoft 365 again" in _state(tmp_path)["last_error"]


@pytest.mark.asyncio
async def test_listing_and_reading_synced_files() -> None:
    await _sync(FakeGraph(), FakeStore())
    listing = onedrive_sync.list_synced_files()
    names = [f["name"] for f in listing["files"]]
    assert names == sorted(names, key=str.lower) and listing["last_run"]
    key = item_key(DRIVE, TXT)
    doc = onedrive_sync.read_synced_file(key)
    assert doc is not None and "net 30" in doc["content"] and "onedrive_key" not in doc["content"]
    assert onedrive_sync.read_synced_file("../../etc:passwd") is None
    assert onedrive_sync.read_synced_file(item_key(DRIVE, "01UNKNOWN")) is None


@pytest.mark.asyncio
async def test_purge_all_clears_everything(tmp_path: Path) -> None:
    store = FakeStore()
    await _sync(FakeGraph(), store)
    assert onedrive_sync.purge_all_synced(store) == 4  # type: ignore[arg-type]
    assert not store.synced_keys() and not list((tmp_path / "docs" / "onedrive").iterdir())
    assert _state(tmp_path)["files"] == {}


# --- token -------------------------------------------------------------------


def _launcher(tmp_path: Path) -> str:
    path = tmp_path / "ms365-mcp-launch.sh"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


@pytest.mark.asyncio
async def test_token_comes_from_the_launcher_and_is_cached(tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    now = [1000.0]

    async def run(launcher: str, scopes: tuple[str, ...]) -> tuple[int, str, str]:
        calls.append((launcher, scopes))
        return 0, json.dumps({"access_token": f"t{len(calls)}", "expires_on": now[0] + 3600}), ""

    token = onedrive_account.launcher_token_provider(_launcher(tmp_path), run=run, clock=lambda: now[0])
    assert await token() == "t1" and await token() == "t1"
    assert calls == [(_launcher(tmp_path), ("Files.Read.All",))]
    now[0] += 3600
    assert await token() == "t2"


@pytest.mark.asyncio
async def test_a_refused_sign_in_is_not_retried_within_a_tick(tmp_path: Path) -> None:
    calls = {"n": 0}

    async def run(launcher: str, scopes: tuple[str, ...]) -> tuple[int, str, str]:
        calls["n"] += 1
        return 1, "", "warming up\nms365-access-token: invalid_grant: AADSTS70000 refused\n"

    token = onedrive_account.launcher_token_provider(_launcher(tmp_path), run=run)
    for _ in range(2):
        with pytest.raises(onedrive_account.OneDriveCredentialMissing, match="invalid_grant"):
            await token()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried(tmp_path: Path) -> None:
    replies = iter([(1, "", "network down"), (0, '{"access_token": "ok", "expires_on": 9e12}', "")])

    async def run(launcher: str, scopes: tuple[str, ...]) -> tuple[int, str, str]:
        return next(replies)

    token = onedrive_account.launcher_token_provider(_launcher(tmp_path), run=run)
    with pytest.raises(onedrive_account.OneDriveAuthTransient):
        await token()
    assert await token() == "ok"


def test_a_missing_launcher_is_reported_up_front(tmp_path: Path) -> None:
    with pytest.raises(onedrive_account.OneDriveCredentialMissing):
        onedrive_account.launcher_token_provider(str(tmp_path / "missing.sh"))


@pytest.mark.asyncio
async def test_the_real_helper_runner_passes_scopes_on_stdin(tmp_path: Path) -> None:
    script = tmp_path / "launcher.sh"
    script.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "--access-token" ] || exit 9\n'
        "read -r input\n"
        'printf \'{"access_token": "%s", "expires_on": 1}\' "$(printf %s "$input" | tr -d \'" {}[]:\' )"\n'
    )
    script.chmod(0o755)
    code, out, _ = await onedrive_account._run_helper(str(script), ("Files.Read.All",))
    assert code == 0 and json.loads(out)["access_token"] == "scopesFiles.Read.All"


# --- retriever ---------------------------------------------------------------


def test_retriever_labels_onedrive_chunks_and_records_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.knowledge import retriever
    from openexecutive.knowledge.review_store import ReviewStore

    review_db = tmp_path / "review.db"
    ReviewStore.initialize_db(review_db)
    store = FakeStore()
    store.add_documents(
        ["Plan text"],
        [{
            "onedrive_key": item_key(DRIVE, DOCX), "type": "onedrive",
            "name": 'Evil] · item x:y · "', "url": "https://contoso-my.sharepoint.com/x",
            "synced_at": "2026-09-01T10:30:00+00:00",
        }],
        ["c1"],
        COLLECTION,
    )
    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr(retriever, "_emit_retrieval_audit", lambda **kw: None)
    block = retriever.retrieve(
        "plan",
        store=store,  # type: ignore[arg-type]
        review_store=ReviewStore(db_path=review_db),
        record_source=lambda *a, **k: recorded.append(a),
    )
    assert "### Synced OneDrive" in block
    label = next(line for line in block.splitlines() if line.startswith("[onedrive"))
    assert label.startswith(f"[onedrive · item {DRIVE}:{DOCX} · synced 2026-09-01 10:30 UTC · ")
    # The name can neither close the label nor add a field of its own.
    assert label.count(" · ") == 3 and label.endswith('"Evil item x:y"]')
    assert ("onedrive", 'Evil] · item x:y · "', "https://contoso-my.sharepoint.com/x") in recorded


# --- the token helper (docker/ms365-access-token.mjs) ---------------------------

_HELPER = Path(__file__).resolve().parents[4] / "docker" / "ms365-access-token.mjs"


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="no node")
def test_token_helper_mints_file_read_tokens_only() -> None:
    import subprocess

    script = f"""
const m = await import({json.dumps(_HELPER.as_uri())});
const fake = {{
  loadTokenCache: async () => {{}},
  getCurrentAccount: async () => ({{ username: "exec@contoso.com" }}),
  msalApp: {{ acquireTokenSilent: async (r) => ({{ accessToken: "AT " + r.scopes.join(" "), expiresOn: new Date(2000000000000) }}) }},
}};
const out = [await m.accessToken({{ scopes: ["Files.Read.All"], authManager: fake }})];
for (const scopes of [["Mail.Send"], [], ["Files.Read.All", "Mail.ReadWrite"]]) {{
  try {{ await m.accessToken({{ scopes, authManager: fake }}); out.push("allowed"); }}
  catch (e) {{ out.push("refused"); }}
}}
try {{ await m.accessToken({{ scopes: ["Files.Read.All"], authManager: {{ ...fake, getCurrentAccount: async () => null }} }}); }}
catch (e) {{ out.push(e.errorCode); }}
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    first, *rest = json.loads(proc.stdout)
    assert first == {
        "access_token": "AT https://graph.microsoft.com/Files.Read.All", "expires_on": 2000000000,
    }
    assert rest == ["refused", "refused", "refused", "no_account"]


def test_token_helper_gets_only_the_launcher_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.knowledge import onedrive_account

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "shh")
    monkeypatch.setenv("MS365_MCP_CLIENT_ID", "client-1")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = onedrive_account._helper_env()
    assert env["MS365_MCP_CLIENT_ID"] == "client-1" and env["PATH"] == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in env and "BACKEND_SHARED_SECRET" not in env
