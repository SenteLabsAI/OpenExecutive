"""Outline sync: isolated collection, watermark, reconcile, collection-scope enforcement."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openexecutive.config import Settings
from openexecutive.knowledge.outline_sync import (
    _BLANK_LISTING_TRUST_AFTER_SKIPS,
    _doc_edited_after,
    _strip_leading_metadata_table,
    fetch_document_body,
    infer_domain,
    list_documents_in_scope,
    purge_document,
    run_outline_sync,
    sanitize_markdown_mentions,
    sanitize_outline_id,
    slugify,
)
from openexecutive.knowledge.store import ChromaDBStore

DOC_NEW = "11111111-1111-1111-1111-111111111111"
DOC_OLD = "22222222-2222-2222-2222-222222222222"
DOC_GONE = "33333333-3333-3333-3333-333333333333"
DOC_KEEP = "44444444-4444-4444-4444-444444444444"
DOC_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DOC_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
COLLECTION_ALLOWED = "cccccccc-cccc-cccc-cccc-cccccccccccc"
COLLECTION_OTHER = "dddddddd-dddd-dddd-dddd-dddddddddddd"


class FakeStore:
    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, Any]]] = {}
        self.delete_calls: list[tuple[str, dict[str, Any]]] = []

    def add_documents(self, texts, metadatas, ids, collection):
        col = self.collections.setdefault(collection, [])
        for t, m, i in zip(texts, metadatas, ids, strict=False):
            col[:] = [r for r in col if r["id"] != i]
            col.append({"id": i, "text": t, "metadata": m})

    def delete_documents(self, collection, where):
        self.delete_calls.append((collection, where))
        col = self.collections.get(collection, [])
        self.collections[collection] = [
            r
            for r in col
            if not all(r["metadata"].get(k) == v for k, v in where.items())
        ]

    def query(self, query_text, collection, domain_filter=None, n_results=5):
        col = self.collections.get(collection, [])
        return [
            {"text": r["text"], "metadata": r["metadata"], "distance": 0.1}
            for r in col[:n_results]
        ]

    def delete_outline_docs(self) -> None:
        self.delete_documents(ChromaDBStore.OUTLINE_COLLECTION, {"type": "outline"})


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    monkeypatch.delenv("OUTLINE_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("OUTLINE_API_KEY", raising=False)
    monkeypatch.delenv("OUTLINE_BASE_URL", raising=False)
    monkeypatch.delenv("OUTLINE_COLLECTION_IDS", raising=False)


@pytest.fixture(autouse=True)
def _fresh_fixture_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # An asyncio.Lock binds to the first event loop that awaits it, and
    # pytest-asyncio gives each test its own loop — swap in a fresh Lock so
    # no test ever sees one bound to (or still held from) another test's loop.
    from openexecutive.cli import fixture_loader

    monkeypatch.setattr(fixture_loader, "_FIXTURE_OP_LOCK", asyncio.Lock())


def test_infer_domain_from_title() -> None:
    assert infer_domain("Q3 finance review") == "finance"
    assert infer_domain("Random wiki page") == "general"


def test_infer_domain_matches_whole_words_only() -> None:
    # "hr" is a substring of "Chrome" — must not tag this as HR.
    assert infer_domain("Chrome extension notes") == "general"
    assert infer_domain("HR onboarding checklist") == "hr"


def test_doc_edited_after() -> None:
    doc = {"updatedAt": "2026-01-02T00:00:00.000Z"}
    assert _doc_edited_after(doc, None) is True
    assert _doc_edited_after(doc, "2026-01-01T00:00:00.000Z") is True
    assert _doc_edited_after(doc, "2026-01-03T00:00:00.000Z") is False


def test_slugify_is_stable() -> None:
    name = slugify("Comp Bands!", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert name.startswith("outline-aaaaaaaa-")
    assert name.endswith(".md")


def test_sanitize_outline_id_accepts_and_rejects() -> None:
    assert sanitize_outline_id(DOC_NEW) == DOC_NEW
    assert sanitize_outline_id("11111111111111111111111111111111") == (
        "11111111-1111-1111-1111-111111111111"
    )
    assert sanitize_outline_id("../evil") is None
    assert sanitize_outline_id("doc-new") is None


def test_sanitize_markdown_mentions_strips_uri() -> None:
    text = "Owner: @[Paul Hirsch](mention://abc/user/def) approved this."
    assert sanitize_markdown_mentions(text) == "Owner: @Paul Hirsch approved this."
    # Leaves ordinary links and text untouched.
    assert sanitize_markdown_mentions("See [docs](https://example.com).") == (
        "See [docs](https://example.com)."
    )


def test_strip_leading_metadata_table_removes_governance_header() -> None:
    # Exact byte pattern confirmed against a live synced doc: a 4-column
    # table (Node Type / Status / Owner / Reviewed), then a `---` divider,
    # then real content.
    text = (
        "| **Node Type** | `Governance_Policy` | **Status** | `Active` |\n"
        "|-----------|-------------------|--------|--------|\n"
        "| **Owner** | @Carlos Sierra | **Reviewed** | `2026-05-04` |\n"
        "\n\n---\n\n"
        "> Controlled vocabulary for the Graph-Based Documentation Framework."
    )
    assert _strip_leading_metadata_table(text) == (
        "> Controlled vocabulary for the Graph-Based Documentation Framework."
    )


def test_strip_leading_metadata_table_removes_two_column_variant() -> None:
    # Different column count (Status/Owner/Version/Tags) — the regex must
    # not be keyed to a specific table shape.
    text = (
        "| **Status** | Active |\n"
        "|--------|--------|\n"
        "| **Owner** | @Paul Hirsch  |\n"
        "| **Version** | 1.0   |\n"
        "| **Tags** | vision, mission, values |\n"
        "\n\n---\n\n"
        "### **I. Vision and Mission**\n\n* **Purpose:** ..."
    )
    assert _strip_leading_metadata_table(text) == (
        "### **I. Vision and Mission**\n\n* **Purpose:** ..."
    )


def test_strip_leading_metadata_table_is_noop_without_a_table() -> None:
    # Most synced docs have no metadata table at all — must pass through
    # unchanged, not eat real content on a false match.
    text = "Just a plain paragraph with no table.\n\nAnd a second one."
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_preserves_table_deeper_in_content() -> None:
    # A table that isn't the very first thing in the body is real content
    # (e.g. a comparison table mid-document) and must survive untouched.
    text = (
        "Some real prose introduces the section.\n\n"
        "| a table | deep in content |\n"
        "|---|---|\n"
        "| x | y |\n\n"
        "More prose follows the table."
    )
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_preserves_real_content_table_without_markers() -> None:
    # A document whose body genuinely IS a table right after the title
    # (e.g. a term/definition glossary) has no Status/Owner/... marker and
    # must NOT be mistaken for the governance boilerplate — destroying it
    # is unrecoverable short of an edit in Outline.
    text = (
        "| Term | Definition |\n"
        "|---|---|\n"
        "| Node | a thing |\n"
        "| Edge | a link |\n"
        "\n---\n\n"
        "more prose"
    )
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_requires_a_divider() -> None:
    # A metadata-shaped table with no divider after it doesn't match the
    # confirmed template closely enough to strip with confidence.
    text = (
        "| **Status** | Active |\n"
        "|---|---|\n"
        "| **Owner** | X |\n"
        "\n"
        "Some real prose right after, no divider."
    )
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_does_not_blank_the_document() -> None:
    # If the table (plus divider) were somehow the entire body, stripping
    # it would leave nothing — keep the original instead of emptying it.
    text = "| **Status** | Active |\n|---|---|\n| **Owner** | X |\n\n---\n\n   \n"
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_handles_crlf() -> None:
    text = (
        "| **Status** | Active |\r\n"
        "|---|---|\r\n"
        "| **Owner** | X |\r\n"
        "\r\n---\r\n\r\n"
        "Real prose."
    )
    assert _strip_leading_metadata_table(text) == "Real prose."


def test_strip_leading_metadata_table_handles_unterminated_final_row() -> None:
    # A table with no trailing newline and no divider (the table is the
    # whole document) must not leave a dangling data row behind — the
    # missing divider means this doesn't match the template anyway, so it's
    # a no-op, not a partial strip.
    text = "| **Status** | Active |\n|---|---|\n| **Owner** | X |"
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_tolerates_leading_blank_lines() -> None:
    text = (
        "\n| **Status** | Active |\n|---|---|\n| **Owner** | X |\n\n---\n\nReal content."
    )
    assert _strip_leading_metadata_table(text) == "Real content."


def test_strip_leading_metadata_table_accepts_underscore_divider() -> None:
    text = "| **Status** | Active |\n|---|---|\n| **Owner** | X |\n\n___\n\nReal content."
    assert _strip_leading_metadata_table(text) == "Real content."


def test_strip_leading_metadata_table_preserves_raci_table_with_single_marker_column() -> (
    None
):
    # A perfectly ordinary business table (a RACI chart) can legitimately
    # have a column named "Owner" and be followed by a divider for
    # unrelated reasons — one matching column isn't enough evidence this is
    # the governance template, which always carries several.
    text = (
        "| Deliverable | Owner |\n"
        "|---|---|\n"
        "| Pricing | Ana |\n"
        "\n***Escalation:*** contact ops.\n"
    )
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_preserves_changelog_with_single_marker_column() -> (
    None
):
    text = (
        "| Version | Date | Changes |\n"
        "|---|---|---|\n"
        "| 1.0 | Jan | Initial |\n"
        "\n---\n\nMore changelog notes.\n"
    )
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_does_not_match_field_name_substrings() -> None:
    # A cell must equal a known field name, not merely contain one as a
    # substring — "Subversion" and "Homeowner" must not trip the
    # "version"/"owner" markers.
    text = (
        "| Item | Detail |\n|---|---|\n| Repo | uses Subversion |\n\n---\n\nprose\n"
    )
    assert _strip_leading_metadata_table(text) == text
    text2 = (
        "| Term | Definition |\n|---|---|\n| HOA | for Homeowner groups |\n\n---\n\nprose\n"
    )
    assert _strip_leading_metadata_table(text2) == text2


def test_strip_leading_metadata_table_does_not_treat_bold_run_as_divider() -> None:
    # A paragraph that starts with a bold/italic run (`***Purpose:***`) must
    # not be mistaken for a `***` thematic-break divider — the divider
    # check has to be anchored to end-of-line, not just "starts with 3+ of
    # the character", or it silently truncates the real paragraph.
    text = (
        "| **Status** | Active |\n|---|---|\n| **Owner** | X |"
        "\n\n***Purpose:*** the real content.\n"
    )
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_rejects_mixed_divider_characters() -> None:
    text = "| **Status** | Active |\n|---|---|\n| **Owner** | X |\n\n-_-*\n\nreal content\n"
    assert _strip_leading_metadata_table(text) == text


def test_strip_leading_metadata_table_ignores_indented_code_block() -> None:
    # 4+ space indentation is a Markdown code block, not a table row.
    text = "    | **Status** | A |\n    |---|---|\n\n---\n\nreal"
    assert _strip_leading_metadata_table(text) == text


def test_ingest_doc_sync_strips_metadata_table_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wiring check: confirms the strip actually runs inside _ingest_doc_sync
    # (both what gets embedded AND what gets written to disk), not just
    # that the standalone helper function works in isolation.
    from openexecutive.knowledge.outline_sync import _ingest_doc_sync

    monkeypatch.setattr(
        "openexecutive.knowledge.outline_sync._docs_dir", lambda: tmp_path
    )
    doc = {"id": DOC_A, "title": "LM Vision, Mission, Values and Vibes"}
    markdown = (
        "| **Status** | Active |\n"
        "|--------|--------|\n"
        "| **Owner** | @Paul Hirsch |\n"
        "\n\n---\n\n"
        "### I. Vision and Mission\n\nThe real content."
    )
    store = FakeStore()
    _ingest_doc_sync(doc, markdown, store)  # type: ignore[arg-type]

    stored = store.collections[ChromaDBStore.OUTLINE_COLLECTION]
    assert len(stored) == 1
    assert "| **Status** |" not in stored[0]["text"]
    assert "The real content." in stored[0]["text"]

    written = next(tmp_path.glob("outline-*.md")).read_text(encoding="utf-8")
    assert "| **Status** |" not in written
    assert "The real content." in written


def test_enabled_without_any_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTLINE_SYNC_ENABLED", "true")
    with pytest.raises(ValueError, match="OUTLINE_API_KEY"):
        Settings(_env_file=None)


def test_enabled_without_collection_ids_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTLINE_SYNC_ENABLED", "true")
    monkeypatch.setenv("OUTLINE_API_KEY", "ol_api_test")
    monkeypatch.setenv("OUTLINE_BASE_URL", "https://outline.test/api")
    with pytest.raises(ValueError, match="OUTLINE_COLLECTION_IDS"):
        Settings(_env_file=None)


def test_fully_configured_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTLINE_SYNC_ENABLED", "true")
    monkeypatch.setenv("OUTLINE_API_KEY", "ol_api_test")
    monkeypatch.setenv("OUTLINE_BASE_URL", "https://outline.test/api")
    monkeypatch.setenv("OUTLINE_COLLECTION_IDS", COLLECTION_ALLOWED)
    settings = Settings(_env_file=None)
    assert settings.outline_collection_ids == [COLLECTION_ALLOWED]


@pytest.mark.asyncio
async def test_run_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.knowledge.outline_sync.get_settings",
        lambda: Settings(_env_file=None, ANTHROPIC_API_KEY="k"),
    )
    stats = await run_outline_sync(store=FakeStore())  # type: ignore[arg-type]
    assert stats["seen"] == 0
    assert stats["updated"] == 0


def _doc(
    doc_id: str,
    edited: str,
    title: str,
    *,
    collection_id: str = COLLECTION_ALLOWED,
    archived: str | None = None,
    deleted: str | None = None,
) -> dict[str, Any]:
    return {
        "id": doc_id,
        "title": title,
        "updatedAt": edited,
        "collectionId": collection_id,
        "archivedAt": archived,
        "deletedAt": deleted,
    }


def _sync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OUTLINE_SYNC_ENABLED", "true")
    monkeypatch.setenv("OUTLINE_API_KEY", "ol_api_test")
    monkeypatch.setenv("OUTLINE_BASE_URL", "https://outline.test/api")
    monkeypatch.setenv("OUTLINE_COLLECTION_IDS", COLLECTION_ALLOWED)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "profile.yaml"))
    state_file = tmp_path / "outline_sync_state.json"
    return state_file


async def _run_sync(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    documents: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
    max_docs: int | None = None,
    body_error_for: set[str] | None = None,
    store: FakeStore | None = None,
) -> tuple[dict[str, int], FakeStore, Path]:
    state_file = _sync_env(tmp_path, monkeypatch)
    if max_docs is not None:
        monkeypatch.setenv("OUTLINE_MAX_DOCS_PER_SCAN", str(max_docs))
    state_file.write_text(
        json.dumps(state or {"watermark": None, "documents": {}}) + "\n",
        encoding="utf-8",
    )
    fail_ids = body_error_for or set()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("documents.list"):
            cid = body.get("collectionId")
            offset = int(body.get("offset", 0))
            limit = int(body.get("limit", 100))
            page = [d for d in documents if d["collectionId"] == cid][
                offset : offset + limit
            ]
            return httpx.Response(200, json={"data": page})
        if request.url.path.endswith("documents.info"):
            doc_id = body.get("id")
            if doc_id in fail_ids:
                return httpx.Response(500, json={"message": "transient"})
            match = next((d for d in documents if d["id"] == doc_id), None)
            if match is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(
                200, json={"data": {**match, "text": f"Body for {match['title']}"}}
            )
        return httpx.Response(404, json={"message": request.url.path})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, headers={"Authorization": "Bearer x"})
    store = store or FakeStore()
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
    return stats, store, state_file


@pytest.mark.asyncio
async def test_sync_indexes_new_and_skips_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats, store, _ = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[
            _doc(DOC_NEW, "2026-06-02T00:00:00.000Z", "OKRs"),
            _doc(DOC_OLD, "2026-01-01T00:00:00.000Z", "Archive"),
        ],
        state={
            "watermark": "2026-06-01T00:00:00.000Z",
            "documents": {
                DOC_OLD: {
                    "last_edited": "2026-01-01T00:00:00.000Z",
                    "filename": "outline-22222222-archive.md",
                    "title": "Archive",
                }
            },
        },
    )
    assert stats["seen"] == 2
    assert stats["updated"] == 1
    assert stats["skipped"] == 1
    outline_rows = store.collections.get(ChromaDBStore.OUTLINE_COLLECTION, [])
    assert outline_rows, "synced document must land in the Outline collection"
    assert all(r["metadata"]["type"] == "outline" for r in outline_rows)
    company_rows = store.collections.get(ChromaDBStore.COMPANY_COLLECTION, [])
    assert not any(r["metadata"].get("type") == "outline" for r in company_rows)


@pytest.mark.asyncio
async def test_watermark_does_not_advance_past_failed_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats, _, state_file = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[
            _doc(DOC_A, "2026-06-02T12:00:00.000Z", "Newest"),
            _doc(DOC_B, "2026-06-02T11:00:00.000Z", "Older"),
        ],
        state={"watermark": "2026-06-01T00:00:00.000Z", "documents": {}},
        body_error_for={DOC_A},
    )
    assert stats["failed"] == 1
    assert stats["updated"] == 1
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["watermark"] == "2026-06-01T00:00:00.000Z"
    assert DOC_A not in saved.get("documents", {})
    assert DOC_B in saved.get("documents", {})


@pytest.mark.asyncio
async def test_over_cap_does_not_drop_overflow_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats, _, state_file = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[
            _doc(DOC_A, "2026-06-02T12:00:00.000Z", "Newest"),
            _doc(DOC_B, "2026-06-02T11:00:00.000Z", "Overflow"),
        ],
        state={"watermark": "2026-06-01T00:00:00.000Z", "documents": {}},
        max_docs=1,
    )
    assert stats["updated"] == 1
    assert stats.get("capped", 0) == 1
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert DOC_B not in saved.get("documents", {})
    # Held exactly at the pre-tick watermark, not just "some lesser value" —
    # a bug that advanced it to any other value short of DOC_B's timestamp
    # would pass a weaker "<" assertion undetected.
    assert saved["watermark"] == "2026-06-01T00:00:00.000Z"

    stats2, _, state_file2 = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[
            _doc(DOC_A, "2026-06-02T12:00:00.000Z", "Newest"),
            _doc(DOC_B, "2026-06-02T11:00:00.000Z", "Overflow"),
        ],
        state=saved,
        max_docs=1,
    )
    assert stats2["updated"] == 1
    saved2 = json.loads(state_file2.read_text(encoding="utf-8"))
    assert DOC_B in saved2.get("documents", {})


@pytest.mark.asyncio
async def test_reconcile_purges_gone_document_chunks_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    stale_name = "outline-33333333-gone.md"
    (docs_dir / stale_name).write_text(
        f"# Gone\n\n<!-- outline_document_id: {DOC_GONE} -->\n\nsecret\n",
        encoding="utf-8",
    )
    store = FakeStore()
    store.add_documents(
        ["stale wiki text"],
        [{"outline_document_id": DOC_GONE, "type": "outline", "filename": stale_name}],
        ["stale-1"],
        ChromaDBStore.OUTLINE_COLLECTION,
    )
    stats, store, state_file = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[_doc(DOC_KEEP, "2026-06-02T00:00:00.000Z", "Keep")],
        state={
            "watermark": "2026-06-01T00:00:00.000Z",
            "documents": {
                DOC_GONE: {
                    "last_edited": "2026-05-01T00:00:00.000Z",
                    "filename": stale_name,
                    "title": "Gone",
                }
            },
        },
        store=store,
    )
    assert stats["purged"] == 1
    assert not (docs_dir / stale_name).exists()
    outline_ids = {
        r["metadata"].get("outline_document_id")
        for r in store.collections.get(ChromaDBStore.OUTLINE_COLLECTION, [])
    }
    assert DOC_GONE not in outline_ids
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert DOC_GONE not in saved.get("documents", {})


@pytest.mark.asyncio
async def test_archived_document_is_treated_as_missing_and_purged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archived document is still returned by the listing call, but must
    be excluded from ingest and purged like a document no longer visible."""
    stats, store, state_file = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[
            _doc(
                DOC_GONE,
                "2026-06-02T00:00:00.000Z",
                "Now archived",
                archived="2026-06-02T00:00:00.000Z",
            )
        ],
        state={
            "watermark": "2026-06-01T00:00:00.000Z",
            "documents": {
                DOC_GONE: {
                    "last_edited": "2026-05-01T00:00:00.000Z",
                    "filename": "outline-33333333-gone.md",
                    "title": "Was active",
                }
            },
        },
    )
    assert stats["updated"] == 0
    assert stats["purged"] == 1
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert DOC_GONE not in saved.get("documents", {})


@pytest.mark.asyncio
async def test_reactivated_unedited_document_is_reingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document purged from state then seen again without an edit must
    not be skipped by the watermark."""
    stats, store, _ = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[_doc(DOC_KEEP, "2026-05-01T00:00:00.000Z", "Returned")],
        state={"watermark": "2026-07-01T00:00:00.000Z", "documents": {}},
    )
    assert stats["updated"] == 1
    assert store.collections.get(ChromaDBStore.OUTLINE_COLLECTION)
    saved = json.loads((tmp_path / "outline_sync_state.json").read_text(encoding="utf-8"))
    assert saved["watermark"] == "2026-07-01T00:00:00.000Z"


@pytest.mark.asyncio
async def test_document_outside_allowlisted_collection_is_never_ingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collection allowlist is the ENTIRE access boundary for Outline
    sync (unlike Notion, there is no per-document share-to-integration
    ACL) — so even if a server-side collectionId filter were somehow
    bypassed or misapplied, a document from an unlisted collection must
    never reach ingest. This handler deliberately ignores the requested
    collectionId to prove the client-side check is what protects us, not
    just trust in the request parameter.
    """
    state_file = _sync_env(tmp_path, monkeypatch)
    state_file.write_text(
        json.dumps({"watermark": None, "documents": {}}) + "\n", encoding="utf-8"
    )
    allowed_doc = _doc(
        DOC_A, "2026-06-02T00:00:00.000Z", "In scope", collection_id=COLLECTION_ALLOWED
    )
    leaked_doc = _doc(
        DOC_B, "2026-06-02T00:00:00.000Z", "Out of scope", collection_id=COLLECTION_OTHER
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("documents.list"):
            return httpx.Response(200, json={"data": [allowed_doc, leaked_doc]})
        if request.url.path.endswith("documents.info"):
            body = json.loads(request.content or b"{}")
            match = next(
                (d for d in (allowed_doc, leaked_doc) if d["id"] == body.get("id")),
                None,
            )
            if match is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(
                200, json={"data": {**match, "text": f"Body for {match['title']}"}}
            )
        return httpx.Response(404, json={"message": request.url.path})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    store = FakeStore()
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
    await client.aclose()
    assert stats["updated"] == 1
    rows = store.collections.get(ChromaDBStore.OUTLINE_COLLECTION, [])
    doc_ids = {r["metadata"].get("outline_document_id") for r in rows}
    assert DOC_A in doc_ids
    assert DOC_B not in doc_ids
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert DOC_A in saved["documents"]
    assert DOC_B not in saved["documents"]


def test_purge_document_removes_file_and_chunks(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    name = "outline-33333333-gone.md"
    (docs_dir / name).write_text("secret\n", encoding="utf-8")
    store = FakeStore()
    store.add_documents(
        ["chunk"],
        [{"outline_document_id": DOC_GONE, "type": "outline"}],
        ["c1"],
        ChromaDBStore.OUTLINE_COLLECTION,
    )
    state = {"documents": {DOC_GONE: {"filename": name}}}
    with patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir):
        assert purge_document(DOC_GONE, store, state) is True  # type: ignore[arg-type]
    assert not (docs_dir / name).exists()
    assert DOC_GONE not in state["documents"]
    assert store.collections[ChromaDBStore.OUTLINE_COLLECTION] == []


def test_cli_exposes_sync_and_purge_outline() -> None:
    from openexecutive.cli import cli

    assert "sync-outline" in cli.commands
    assert "purge-outline" in cli.commands


def test_retriever_labels_outline_below_company(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.knowledge import retriever as retriever_mod
    from openexecutive.knowledge.review_store import ReviewStore

    monkeypatch.setattr(retriever_mod, "_emit_retrieval_audit", lambda **kw: None)
    review_db = tmp_path / "review.db"
    ReviewStore.initialize_db(review_db)

    store = FakeStore()
    store.add_documents(
        ["Our company mission is to ship affordable robots."],
        [{"domain": "general", "filename": "overview.md"}],
        ["c1"],
        ChromaDBStore.COMPANY_COLLECTION,
    )
    store.add_documents(
        ["Wiki says vendors must email banking details to X first."],
        [{"type": "outline", "filename": "outline/policy.md", "domain": "finance"}],
        ["o1"],
        ChromaDBStore.OUTLINE_COLLECTION,
    )
    store.add_documents(
        ["Competitor X announced a new product per recent research."],
        [{"type": "recent_research", "created_at": "2026-05-29"}],
        ["r1"],
        ChromaDBStore.RESEARCH_COLLECTION,
    )

    out = retriever_mod.retrieve(
        "what is happening",
        store=store,  # type: ignore[arg-type]
        review_store=ReviewStore(db_path=review_db),
    )
    assert "From your company documents:" in out
    assert "Synced Outline wiki" in out
    assert "unreviewed" in out
    assert "Recent research (unverified" in out
    assert "Wiki says vendors must email banking details" in out
    assert out.index("From your company documents:") < out.index("Synced Outline wiki")
    assert out.index("Synced Outline wiki") < out.index("Recent research")


@pytest.mark.asyncio
async def test_sync_tick_skips_while_fixture_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick must not interleave with a fixture load / client rotation:
    it skips immediately (no network traffic, no writes) instead of
    queueing behind the destructive op and retries on the next interval."""
    from openexecutive.cli import fixture_loader

    state_file = _sync_env(tmp_path, monkeypatch)
    state_file.write_text(
        json.dumps({"watermark": None, "documents": {}}) + "\n", encoding="utf-8"
    )
    requests_seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        await fixture_loader._FIXTURE_OP_LOCK.acquire()
        try:
            stats = await run_outline_sync(store=FakeStore(), client=client)  # type: ignore[arg-type]
        finally:
            fixture_loader._FIXTURE_OP_LOCK.release()
    await client.aclose()
    assert requests_seen == []
    assert stats == {
        "seen": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "purged": 0,
        "capped": 0,
    }


@pytest.mark.asyncio
async def test_sync_discards_tick_when_active_client_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation that swaps the active client between the fetch phase and
    the write phase must abort the tick: nothing fetched under the old
    client may be written into the new client's collection or state."""
    calls = {"n": 0}

    def _generation(settings: Any) -> str:
        calls["n"] += 1
        return "alpha" if calls["n"] == 1 else "beta"

    monkeypatch.setattr("openexecutive.clients.slots.get_active_client", _generation)
    stats, store, state_file = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[_doc(DOC_A, "2026-06-01T00:00:00.000Z", "A")],
    )
    assert calls["n"] == 2
    assert stats["seen"] == 1  # the fetch phase ran...
    assert stats["updated"] == 0  # ...but nothing was written
    assert store.collections.get(ChromaDBStore.OUTLINE_COLLECTION, []) == []
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["documents"] == {}


@pytest.mark.asyncio
async def test_truncated_listing_skips_reconcile_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected-shape response must skip reconciliation (keeping
    unseen documents) and grow the consecutive-skip counter; a well-formed
    listing reconciles and resets it."""
    state_file = _sync_env(tmp_path, monkeypatch)
    state_file.write_text(
        json.dumps(
            {
                "watermark": None,
                "documents": {
                    DOC_GONE: {
                        "last_edited": "2026-01-01T00:00:00.000Z",
                        "filename": "outline-33333333-gone.md",
                        "title": "Gone",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    malformed = {"on": True}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("documents.list"):
            if malformed["on"]:
                return httpx.Response(200, json={"data": "not-a-list"})
            return httpx.Response(
                200,
                json={"data": [_doc(DOC_A, "2026-06-01T00:00:00.000Z", "A")]},
            )
        if request.url.path.endswith("documents.info"):
            return httpx.Response(
                200, json={"data": {**_doc(DOC_A, "2026-06-01T00:00:00.000Z", "A"), "text": "Body"}}
            )
        return httpx.Response(404, json={"message": request.url.path})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    store = FakeStore()
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert stats["purged"] == 0
        assert DOC_GONE in state["documents"]
        assert state["reconcile_skips"] == 1

        malformed["on"] = False
        stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert stats["purged"] == 1
        assert DOC_GONE not in state["documents"]
        assert state["reconcile_skips"] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_document_body_raises_on_malformed_response() -> None:
    """A `documents.info` response missing "text" must raise, not return "" —
    an empty-string return would read as a genuinely blank document and let
    the caller overwrite real, previously-synced content with a stub."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": DOC_A}})  # no "text"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="unexpected response shape"):
        await fetch_document_body(
            client, "https://outline.test/api", DOC_A, collection_ids={COLLECTION_ALLOWED}
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_document_body_raises_on_collection_mismatch() -> None:
    """A document that has moved out of the allowlist between the listing
    call and this fetch must be rejected — the listing's collectionId
    check is not the only enforcement point."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"id": DOC_A, "collectionId": COLLECTION_OTHER, "text": "Body"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="no longer in an allowed collection"):
        await fetch_document_body(
            client, "https://outline.test/api", DOC_A, collection_ids={COLLECTION_ALLOWED}
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_document_body_raises_on_id_mismatch() -> None:
    """A response for a different document than requested (server bug or a
    misbehaving/compromised host) must never be silently accepted."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"id": DOC_B, "collectionId": COLLECTION_ALLOWED, "text": "Body"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="expected"):
        await fetch_document_body(
            client, "https://outline.test/api", DOC_A, collection_ids={COLLECTION_ALLOWED}
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_malformed_document_body_does_not_overwrite_existing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a malformed documents.info response on a re-sync tick
    must not delete or replace a document's real, already-ingested chunks
    with a near-empty stub, and must not be recorded as updated."""
    stats1, store, state_file = await _run_sync(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        documents=[_doc(DOC_A, "2026-06-01T00:00:00.000Z", "Real content")],
    )
    assert stats1["updated"] == 1
    original_rows = list(store.collections.get(ChromaDBStore.OUTLINE_COLLECTION, []))
    assert original_rows and "Body for Real content" in original_rows[0]["text"]

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("documents.list"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        _doc(DOC_A, "2026-06-02T00:00:00.000Z", "Real content")
                    ]
                },
            )
        if request.url.path.endswith("documents.info"):
            assert body.get("id") == DOC_A
            return httpx.Response(200, json={"data": {"id": DOC_A}})  # no "text"
        return httpx.Response(404, json={"message": request.url.path})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch(
            "openexecutive.knowledge.outline_sync._docs_dir",
            return_value=tmp_path / "docs",
        ),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        stats2 = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
    await client.aclose()

    assert stats2["updated"] == 0
    assert stats2["failed"] == 1
    rows_after = store.collections.get(ChromaDBStore.OUTLINE_COLLECTION, [])
    assert rows_after == original_rows, "real content must survive a malformed re-fetch"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["documents"][DOC_A]["last_edited"] == "2026-06-01T00:00:00.000Z", (
        "must not record the new updatedAt — the failed fetch must be retried"
    )


@pytest.mark.asyncio
async def test_list_documents_in_scope_does_not_hang_when_every_row_is_filtered() -> None:
    """If every returned row is outside the allowlist (server ignores or
    misapplies the collectionId filter), the listing must terminate via
    the iteration cap instead of looping forever on a stalled `offset`."""
    calls = {"n": 0}
    noise_page = [
        {"id": f"{i:08x}-0000-0000-0000-000000000000", "collectionId": COLLECTION_OTHER}
        for i in range(100)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": noise_page})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()):
        documents, truncated = await list_documents_in_scope(
            client,
            "https://outline.test/api",
            collection_ids={COLLECTION_ALLOWED},
            max_docs=2000,
        )
    await client.aclose()
    assert documents == []
    assert truncated is not None
    assert "iteration cap" in truncated
    # Bounded: must not have kept requesting pages indefinitely.
    assert calls["n"] < 100


@pytest.mark.asyncio
async def test_list_documents_in_scope_paginates_across_multiple_pages() -> None:
    """A legitimate multi-page listing (more documents than one page size)
    must still return everything in scope, not just the first page."""
    all_docs = [
        {
            "id": f"{i:08x}-0000-0000-0000-000000000000",
            "collectionId": COLLECTION_ALLOWED,
            "title": f"doc {i}",
        }
        for i in range(150)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        offset = int(body.get("offset", 0))
        limit = int(body.get("limit", 100))
        return httpx.Response(200, json={"data": all_docs[offset : offset + limit]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()):
        documents, truncated = await list_documents_in_scope(
            client,
            "https://outline.test/api",
            collection_ids={COLLECTION_ALLOWED},
            max_docs=2000,
        )
    await client.aclose()
    assert truncated is None
    assert {d["id"] for d in documents} == {d["id"] for d in all_docs}


@pytest.mark.asyncio
async def test_blank_listing_escalates_to_purge_after_repeated_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed but empty listing (e.g. the allowlist was narrowed, or
    every allowlisted collection is now empty) must not leave revoked
    documents stuck forever — after enough consecutive ticks see the same
    empty listing, it must trust it and purge."""
    state_file = _sync_env(tmp_path, monkeypatch)
    state_file.write_text(
        json.dumps(
            {
                "watermark": "2026-06-01T00:00:00.000Z",
                "documents": {
                    DOC_GONE: {
                        "last_edited": "2026-05-01T00:00:00.000Z",
                        "filename": "outline-33333333-gone.md",
                        "title": "Gone",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("documents.list"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"message": request.url.path})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    store = FakeStore()
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        for i in range(_BLANK_LISTING_TRUST_AFTER_SKIPS - 1):
            stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
            assert stats["purged"] == 0, f"tick {i}: must still be skipping"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            assert DOC_GONE in state["documents"]

        stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
        assert stats["purged"] == 1, "must escalate to trust + purge on the Nth consecutive tick"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert DOC_GONE not in state["documents"]
        assert state["reconcile_skips"] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_truncated_ticks_never_precharge_the_blank_listing_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated/malformed listing must never count toward the
    blank-listing escalation streak — otherwise a deployment that hits the
    safety cap (any wiki with more in-scope documents than the cap) would
    arrive "pre-charged", and the very first genuinely empty listing would
    trigger a full mass purge instead of requiring several in a row."""
    state_file = _sync_env(tmp_path, monkeypatch)
    state_file.write_text(
        json.dumps(
            {
                "watermark": "2026-06-01T00:00:00.000Z",
                "documents": {
                    DOC_GONE: {
                        "last_edited": "2026-05-01T00:00:00.000Z",
                        "filename": "outline-33333333-gone.md",
                        "title": "Gone",
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mode = {"value": "truncated"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("documents.list"):
            if mode["value"] == "truncated":
                return httpx.Response(200, json={"data": "not-a-list"})
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"message": request.url.path})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    store = FakeStore()
    with (
        patch("openexecutive.knowledge.outline_sync._state_path", return_value=state_file),
        patch("openexecutive.knowledge.outline_sync._docs_dir", return_value=docs_dir),
        patch("openexecutive.knowledge.outline_sync._sleep", new=AsyncMock()),
    ):
        # Two truncated ticks — must not move the blank-listing streak at all.
        for _ in range(2):
            stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
            assert stats["purged"] == 0
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["blank_listing_skips"] == 0
        assert DOC_GONE in state["documents"]

        # A single well-formed empty listing right after must still be
        # treated as the FIRST blank tick, not the third.
        mode["value"] = "blank"
        stats = await run_outline_sync(store=store, client=client)  # type: ignore[arg-type]
        assert stats["purged"] == 0, "one blank tick must not be enough to purge"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["blank_listing_skips"] == 1
        assert DOC_GONE in state["documents"]
    await client.aclose()
