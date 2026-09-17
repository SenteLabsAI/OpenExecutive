"""``reset_all_state`` must re-bootstrap the built-in scheduled actions
that ``api/main.py`` enqueues at process startup.

Without this, the reset wipes ``scheduled_actions`` (correctly) but the
bootstrap functions (``seed_principal_briefs``, ``bootstrap_cadences``,
``bootstrap_nudge_scan``) only run on API boot, so Today stays blank
until the next deploy/restart: no morning brief, no EoD digest, no
reflection, no department check-ins. That made the demo-page reset feel
broken even after wiping run/audit history.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.cli import fixture_loader
from openexecutive.knowledge.store import ChromaDBStore


@pytest.fixture()
def settings_stub(tmp_path: Path) -> Any:
    profile_path = tmp_path / "company" / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("name: ''\n")
    return type(
        "S",
        (),
        {
            "vector_store_path": tmp_path / "chroma",
            "company_profile_path": profile_path,
            "honcho_workspace_id": "openexec",
        },
    )()


@pytest.fixture(autouse=True)
def _isolate_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every DB the reset touches at tmp_path and initialize schemas."""
    from openexecutive.departments import store as dept_store
    from openexecutive.knowledge import review_store as rs_mod
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    episodic_path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", episodic_path)
    # The reset wipes and re-registers knowledge review state. Its module
    # DB_PATH default is bound at import, so without this every test in this
    # file writes review rows into the real ./episodic_memory.db.
    monkeypatch.setattr(rs_mod, "DB_PATH", episodic_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    monkeypatch.setattr(dept_store, "DB_PATH", tmp_path / "depts.db")
    # ``initialize_db``'s ``db_path`` default is bound at def time, so a
    # bare ``initialize_db()`` would write the schema to the host DB, not
    # the monkeypatched tmp file. Pass the path explicitly.
    episodic.initialize_db(episodic_path)
    # The reset wipes alert/audit/eval/workflow tables too — initialize
    # their schemas here so the DELETE pass doesn't fail on a missing
    # table against the freshly-created tmp DB.
    from openexecutive.alerts import store as alerts_store
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.evals.persistence import (
        initialize_eval_runs_db,
        initialize_user_scenarios_db,
    )
    from openexecutive.workflows.persistence import initialize_runs_db

    alerts_store.initialize_db(episodic_path)
    initialize_runs_db(episodic_path)
    initialize_eval_runs_db(episodic_path)
    initialize_user_scenarios_db(episodic_path)
    AuditLogger(db_path=episodic_path)  # constructor creates audit_log
    people_store.initialize_db()
    dept_store.initialize_db()
    return episodic_path


@pytest.fixture(autouse=True)
def _stub_honcho(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset's Honcho hop is irrelevant to this test — stub it to a no-op."""
    from openexecutive.memory import honcho_client

    async def _noop(workspace_id: str | None = None) -> None:
        return None

    monkeypatch.setattr(honcho_client, "delete_workspace_and_reset_client", _noop)


def _scheduled_kinds(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT kind FROM scheduled_actions WHERE status = 'pending'"
            )
        }


def test_reset_re_enqueues_principal_briefs_and_dept_cadences(
    settings_stub: Any, _isolate_dbs: Path
) -> None:
    """After reset, the same three principal brief kinds and at least one
    ``dept_cadence`` row should be back in ``scheduled_actions``.

    These are the rows ``api/main.py`` bootstraps at startup. Before the
    fix, the reset deleted them and they didn't return until the process
    restarted.
    """
    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        result = asyncio.run(fixture_loader.reset_all_state(settings_stub))

    assert result["reset"] is True

    kinds = _scheduled_kinds(_isolate_dbs)

    # Principal briefs — all three idempotently seeded by seed_principal_briefs.
    assert "principal_brief_morning" in kinds
    assert "principal_brief_eod" in kinds
    assert "executive_reflection" in kinds

    # Department cadences — the 8 default departments each carry a
    # check_in cadence spec, so bootstrap_cadences enqueues one row apiece.
    assert "dept_cadence" in kinds


def test_reset_bootstraps_are_idempotent_on_second_call(
    settings_stub: Any, _isolate_dbs: Path
) -> None:
    """Calling reset twice must not duplicate the bootstrapped rows.

    ``seed_principal_briefs`` / ``bootstrap_cadences`` both check for a
    pending row before inserting; the reset relies on that to stay clean
    across repeated presses of the Reset button.
    """
    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        asyncio.run(fixture_loader.reset_all_state(settings_stub))
        asyncio.run(fixture_loader.reset_all_state(settings_stub))

    with sqlite3.connect(str(_isolate_dbs)) as conn:
        # Exactly one of each principal brief, never duplicated.
        for kind in (
            "principal_brief_morning",
            "principal_brief_eod",
            "executive_reflection",
        ):
            count = conn.execute(
                "SELECT COUNT(*) FROM scheduled_actions "
                "WHERE kind = ? AND status = 'pending'",
                (kind,),
            ).fetchone()[0]
            assert count == 1, f"{kind} duplicated across resets: {count} rows"

        # ``dept_cadence`` is one row per department with a check_in spec —
        # the count must match after one reset and stay flat after the
        # second. Without ``_has_pending_cadence``'s per-slug guard, the
        # second reset would double the row count.
        cadence_rows = conn.execute(
            "SELECT department, COUNT(*) FROM scheduled_actions "
            "WHERE kind = 'dept_cadence' AND status = 'pending' "
            "GROUP BY department"
        ).fetchall()
        assert cadence_rows, "expected at least one dept_cadence row after reset"
        for slug, count in cadence_rows:
            assert count == 1, f"dept_cadence for {slug} duplicated: {count} rows"


def test_reset_clears_review_state_and_reregisters_defaults(
    settings_stub: Any, _isolate_dbs: Path
) -> None:
    """A factory reset must not inherit the previous operator's review decisions.

    A rejection suppresses retrieval, so a stale one would keep silently
    withholding knowledge on the reset box. After the wipe the shipped docs are
    re-registered as trusted defaults rather than left as an empty table.
    """
    from openexecutive.knowledge import review_store as rs_mod
    from openexecutive.knowledge.review_store import ReviewStatus, ReviewStore

    ReviewStore.initialize_db(_isolate_dbs)
    store = ReviewStore(db_path=_isolate_dbs)
    ReviewStore.sync_builtin_registrations(_isolate_dbs)

    stale = store.list_items(limit=1)[0]
    store.set_status(stale.item_id, ReviewStatus.REJECTED, "previous operator's call")
    store.add_annotation(stale.item_id, stale.domain, "stale correction")
    assert store.get_withheld_keys(rs_mod.ContentType.BUILTIN)

    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        asyncio.run(fixture_loader.reset_all_state(settings_stub))

    counts = store.count_by_status()
    assert counts["rejected"] == 0
    assert counts["pending"] == 0
    assert counts["approved"] == counts["total"] > 0  # re-registered as defaults
    assert store.get_withheld_keys(rs_mod.ContentType.BUILTIN) == set()
    assert store.list_annotations(active_only=False) == []


def test_reset_survives_a_db_without_review_tables(
    settings_stub: Any, _isolate_dbs: Path
) -> None:
    """The review schema is not guaranteed to exist on the reset path."""
    from openexecutive.knowledge import review_store as rs_mod

    with sqlite3.connect(_isolate_dbs) as conn:
        conn.execute("DROP TABLE IF EXISTS review_annotations")
        conn.execute("DROP TABLE IF EXISTS review_items")

    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        asyncio.run(fixture_loader.reset_all_state(settings_stub))

    assert rs_mod.ReviewStore(db_path=_isolate_dbs).count_by_status()["total"] > 0


def test_reset_does_not_unsuppress_surviving_user_knowledge(
    settings_stub: Any, _isolate_dbs: Path
) -> None:
    """A factory reset must not silently return rejected content to RAG.

    The reset wipes `review_items` but does NOT delete user-authored files
    under `knowledge/builtin/` or their indexed chunks — they live inside the
    installed package. Without re-registration the rejection vanishes while
    the content survives, so the doc becomes retrievable again with no review
    row at all: un-suppressed, and invisible in the queue.
    """
    from openexecutive.knowledge import review_store as rs_mod
    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH
    from openexecutive.knowledge.review_store import (
        ContentType,
        ReviewStatus,
        ReviewStore,
        build_item_id,
    )

    upload = BUILTIN_KNOWLEDGE_PATH / "hr" / "severance_terms_draft.md"
    upload.write_text("# draft, legally unsound", encoding="utf-8")
    item_id = build_item_id(ContentType.BUILTIN, "hr", upload.name)
    try:
        ReviewStore.initialize_db(_isolate_dbs)
        ReviewStore.sync_builtin_registrations(_isolate_dbs)
        store = ReviewStore(db_path=_isolate_dbs)
        store.register(
            item_id=item_id,
            content_type=ContentType.BUILTIN,
            domain="hr",
            filename=upload.name,
        )
        store.set_status(item_id, ReviewStatus.REJECTED, "legally unsound")
        assert ("hr", upload.name) in store.get_withheld_keys(ContentType.BUILTIN)

        with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
             patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
            asyncio.run(fixture_loader.reset_all_state(settings_stub))

        survivor = store.get_item(item_id)
        assert survivor is not None, "surviving content must keep a review row"
        assert survivor.status == ReviewStatus.PENDING
        assert survivor.trusted_default is False
        # Still withheld from retrieval, and visible in the queue.
        assert ("hr", upload.name) in store.get_withheld_keys(ContentType.BUILTIN)
        assert any(i.item_id == item_id for i in store.list_items(limit=500))
        assert rs_mod  # module referenced for the isolation fixture
    finally:
        upload.unlink()


def test_reset_does_not_requeue_shipped_docs(
    settings_stub: Any, _isolate_dbs: Path
) -> None:
    """The re-registration must only sweep in content the manifest disowns."""
    from openexecutive.knowledge.review_store import ReviewStore

    with patch.object(ChromaDBStore, "delete_company_docs", lambda self: None), \
         patch.object(ChromaDBStore, "delete_documents", lambda self, **kw: None):
        asyncio.run(fixture_loader.reset_all_state(settings_stub))

    counts = ReviewStore(db_path=_isolate_dbs).count_by_status()
    assert counts["pending"] == 0, "shipped docs must come back as trusted defaults"
    assert counts["approved"] == counts["total"] > 0
