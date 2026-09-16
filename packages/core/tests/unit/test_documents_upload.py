"""Unit tests for POST/DELETE /documents.

Regression guards, in the order they were found:

1. `domain` was declared as a bare default (`domain: str = "general"`), which
   FastAPI parses as a query parameter. The UI sends it as a form field, so it
   was silently dropped and every upload landed under "general".

2. (#113) The handler staged the upload in a `tempfile.NamedTemporaryFile` and
   passed *that* path to `ingest_file`, which derives chunk metadata AND the
   chunk id from it. The id is an MD5 of a freshly random path, so the id-keyed
   upsert never collided: re-uploads appended a whole duplicate chunk set, and
   the stored `filename` was `tmpXXXXXXXX.md`, which
   `DELETE /documents/{filename}` could never match — it unlinked the on-disk
   copy, returned 200, and left every chunk in the store forever.

3. (#114) `domain` was an unvalidated free string, so `domain=finanace`
   returned 200 and indexed the document where no specialist filters, making it
   permanently unretrievable with no error and no way to notice.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import documents

from ._fake_store import FakeStore as _CapturingStore


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml"))
    # Don't fan out to the real proactive-alerts pipeline during the test.
    monkeypatch.setattr(
        "openexecutive.alerts.pipeline.schedule_evaluation",
        lambda *a, **k: None,
    )

    app = FastAPI()
    app.include_router(documents.router)
    app.state.store = _CapturingStore()
    return TestClient(app)


def _upload(client: TestClient, **data: str) -> Any:
    files = {"file": ("plan.md", io.BytesIO(b"# Plan\nGrow revenue 30%."), "text/markdown")}
    return client.post("/documents", files=files, data=data)


def test_domain_from_form_field_is_honored(client: TestClient) -> None:
    resp = _upload(client, domain="finance")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "finance"
    assert body["chunks_indexed"] >= 1

    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.added, "expected at least one indexed chunk"
    assert all(m["domain"] == "finance" for m in store.added)


def test_domain_defaults_to_general_when_omitted(client: TestClient) -> None:
    resp = _upload(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == "general"
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert all(m["domain"] == "general" for m in store.added)


def test_get_document_returns_extracted_text(client: TestClient) -> None:
    # Upload writes the original file to disk; the viewer reads it back.
    assert _upload(client).status_code == 200

    resp = client.get("/documents/plan.md")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "plan.md"
    assert "Grow revenue 30%." in body["content"]


def test_get_document_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/documents/does_not_exist.md")
    assert resp.status_code == 404


def test_get_document_rejects_dotfile(client: TestClient) -> None:
    # The filename guard rejects dotfiles / non-bare names so a crafted path
    # can't escape the docs directory. (URL-encoded `../` is additionally
    # collapsed by path normalization before it ever reaches the handler.)
    resp = client.get("/documents/.env")
    assert resp.status_code == 400


# ── #113: stable, filename-derived document identity ──────────────────────


def test_reupload_upserts_instead_of_duplicating(client: TestClient) -> None:
    """The bug: each upload staged to a fresh temp path, so chunk ids (an MD5
    of that path) never collided and the upsert always inserted."""
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]

    assert _upload(client).status_code == 200
    after_first = set(store.rows)
    assert after_first, "expected at least one indexed chunk"

    assert _upload(client).status_code == 200

    assert set(store.rows) == after_first, "re-upload must reuse the same chunk ids"
    assert len(store.rows) == len(after_first), "re-upload must not grow the collection"


def test_indexed_under_real_filename_not_temp_path(client: TestClient) -> None:
    assert _upload(client).status_code == 200
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]

    assert store.filenames() == {"plan.md"}
    assert {m["source"] for m in store.added} == {"plan.md"}
    # The specific failure mode: identity taken from the staging file.
    assert not any(m["filename"].startswith("tmp") for m in store.added)


def test_delete_removes_chunks_from_the_index(client: TestClient) -> None:
    """The bug: DELETE matched on `filename`, which held the temp name, so it
    removed nothing, unlinked the on-disk copy and still returned 200 —
    leaving the chunks permanently unreachable."""
    assert _upload(client).status_code == 200
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows

    resp = client.delete("/documents/plan.md")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": "plan.md"}
    assert store.rows == {}, "DELETE must drop the document's chunks, not just the file"


def test_delete_leaves_other_documents_indexed(client: TestClient) -> None:
    files = {"file": ("other.md", io.BytesIO(b"# Other\nUnrelated content."), "text/markdown")}
    assert client.post("/documents", files=files, data={"domain": "hr"}).status_code == 200
    assert _upload(client).status_code == 200

    assert client.delete("/documents/plan.md").status_code == 200

    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.filenames() == {"other.md"}


# ── #114: domain validation ───────────────────────────────────────────────


def test_unknown_domain_is_rejected(client: TestClient) -> None:
    """A typo used to return 200 and index the document where no specialist
    filters — silently unretrievable, with nothing to reveal it."""
    resp = _upload(client, domain="finanace")

    assert resp.status_code == 400
    assert "finanace" in resp.json()["detail"]
    store: _CapturingStore = client.app.state.store  # type: ignore[attr-defined]
    assert store.rows == {}, "a rejected upload must not be indexed"


def test_rejected_domain_does_not_write_the_file(client: TestClient, tmp_path: Path) -> None:
    assert _upload(client, domain="nonsense").status_code == 400
    assert not (tmp_path / "company" / "docs" / "plan.md").exists()


@pytest.mark.parametrize(
    "domain",
    ["strategy", "finance", "hr", "legal", "operations", "marketing", "board", "product", "general"],
)
def test_every_known_domain_is_accepted(client: TestClient, domain: str) -> None:
    resp = _upload(client, domain=domain)

    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == domain
