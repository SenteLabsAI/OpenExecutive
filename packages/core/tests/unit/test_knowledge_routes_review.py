"""The write path must not let user content inherit shipped trust."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from openexecutive.api.main import create_app  # noqa: E402
from openexecutive.knowledge.review_store import (  # noqa: E402
    ContentType,
    ReviewStatus,
    ReviewStore,
    build_item_id,
)


def _shipped_failure_pair() -> tuple[str, str]:
    from pathlib import PurePosixPath

    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    for rel in sorted(SHIPPED_BUILTIN_FILES):
        p = PurePosixPath(rel)
        if p.parts[0] == "failures":
            return p.parent.name, p.name
    raise AssertionError("manifest has no failures/ entries")


@pytest.fixture(autouse=True)
def _isolate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    import openexecutive.audit as audit

    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


@pytest.fixture
def review_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviewStore:
    db = tmp_path / "review.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db, raising=False)
    monkeypatch.setattr("openexecutive.knowledge.review_store.DB_PATH", db, raising=False)
    return ReviewStore(db_path=db)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    store = MagicMock()
    store.query.side_effect = lambda **kw: []
    monkeypatch.setattr(
        "openexecutive.api.routes.knowledge._get_store", lambda _request: store
    )

    async def _fake_ingest(path: Any, _store: Any, **kw: Any) -> int:
        return 1

    monkeypatch.setattr(
        "openexecutive.knowledge.loader.ingest_builtin_file", _fake_ingest
    )
    return TestClient(create_app())


def test_upload_cannot_inherit_a_shipped_failure_docs_trust(
    client: TestClient, review_db: ReviewStore
) -> None:
    """The HIGH finding, as a regression test.

    Uploading a built-in doc named after a shipped FAILURE case study used to
    land on that doc's row and inherit `approved` + `trusted_default = 1` —
    trusted, never queued, and labelled "Ships with Open Executive".
    """
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH

    domain, filename = _shipped_failure_pair()
    written = BUILTIN_KNOWLEDGE_PATH / domain / filename
    assert not written.exists(), "fixture assumes the upload path is free"
    try:
        res = client.post(
            "/knowledge/builtin",
            json={"domain": domain, "filename": filename, "content": "# mine"},
        )
        assert res.status_code == 200, res.text

        upload = review_db.get_item(build_item_id(ContentType.BUILTIN, domain, filename))
        assert upload is not None
        assert upload.trusted_default is False
        assert upload.status == ReviewStatus.PENDING

        shipped = review_db.get_item(build_item_id(ContentType.FAILURE, domain, filename))
        assert shipped is not None
        assert shipped.trusted_default is True
        assert shipped.status == ReviewStatus.APPROVED
    finally:
        written.unlink(missing_ok=True)


def test_upload_is_refused_when_the_id_is_already_a_trusted_default(
    client: TestClient, review_db: ReviewStore
) -> None:
    """Defence in depth: never write onto a shipped row, whatever the namespace."""
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH

    # Forge the pre-fix situation: a trusted row sitting in the upload namespace.
    domain, filename = "board", "forged_shipped.md"
    item_id = build_item_id(ContentType.BUILTIN, domain, filename)
    ReviewStore.initialize_db(review_db._db_path)
    import sqlite3

    with sqlite3.connect(review_db._db_path) as conn:
        conn.execute(
            "INSERT INTO review_items (item_id, content_type, domain, filename, "
            "status, trusted_default, registered_at, last_modified_at) "
            "VALUES (?, 'builtin', ?, ?, 'approved', 1, '2025-01-01', '2025-01-01')",
            (item_id, domain, filename),
        )

    written = BUILTIN_KNOWLEDGE_PATH / domain / filename
    try:
        res = client.post(
            "/knowledge/builtin",
            json={"domain": domain, "filename": filename, "content": "# mine"},
        )
        assert res.status_code == 409
        assert not written.exists(), "refused upload must not have been written"
    finally:
        written.unlink(missing_ok=True)


def test_user_failure_doc_is_registered_and_blockable(
    client: TestClient, review_db: ReviewStore
) -> None:
    """A user-authored failure case study used to have no review row at all.

    `retrieve_failures` applied a gate that could never reach it, so no SME
    decision could block it.
    """
    from openexecutive.knowledge.loader import FAILURES_KNOWLEDGE_PATH

    written = FAILURES_KNOWLEDGE_PATH / "strategy" / "my_case_study.md"
    try:
        res = client.post(
            "/knowledge/failures",
            json={"domain": "strategy", "filename": written.name, "content": "# mine"},
        )
        assert res.status_code == 200, res.text

        item_id = build_item_id(ContentType.FAILURE, "strategy", written.name)
        item = review_db.get_item(item_id)
        assert item is not None, "failure uploads must be registered for review"
        assert item.trusted_default is False
        # Registered `pending`, so the gate can actually reach it.
        assert ("strategy", written.name) in review_db.get_withheld_keys(
            ContentType.FAILURE
        )
    finally:
        written.unlink(missing_ok=True)
