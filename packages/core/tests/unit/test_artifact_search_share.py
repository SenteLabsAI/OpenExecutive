"""Documents page search (`GET /artifacts?q=`) and Share by link
(`orchestrator/artifact_shares.py`, the share routes, `/shared-documents`).

Route handlers are called directly, as in test_artifact_routes.py, against
one temp SQLite file holding the alerts and workflow_runs tables.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response

from openexecutive.alerts import store as alerts_store
from openexecutive.api.routes import artifacts as artifacts_route
from openexecutive.orchestrator import artifact_records, artifact_shares
from openexecutive.orchestrator.artifact_records import Viewer
from openexecutive.workflows import persistence as wf_persistence

PRINCIPAL = Viewer(person_id=1, is_principal=True)
TEAMMATE = Viewer(person_id=2)
OTHER = Viewer(person_id=3)
_REQ: Any = object()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db_path = tmp_path / "episodic.db"
    alerts_store.initialize_db(db_path)
    wf_persistence.initialize_runs_db(db_path)
    monkeypatch.setattr(alerts_store, "DB_PATH", db_path)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db_path)
    who: dict[str, Any] = {"viewer": PRINCIPAL, "left": set(), "path": db_path}
    monkeypatch.setattr(artifacts_route, "_viewer", lambda request: who["viewer"])
    # The roster: 1 is the principal, 2 and 3 teammates; "left" are archived.
    by_id = {1: PRINCIPAL, 2: TEAMMATE, 3: OTHER}
    monkeypatch.setattr(
        "openexecutive.people.store.get_person",
        lambda pid: SimpleNamespace(archived=pid in who["left"], is_principal=by_id[pid].is_principal)
        if pid in by_id else None,
    )

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("openexecutive.orchestrator.artifact_tools.unindex_artifact", _noop)
    audited: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audited.append((event_type, kw)),
    )
    who["audited"] = audited
    return who


def _draft(db: dict[str, Any], key: str, headline: str, body: str, **kw: Any) -> str:
    aid = alerts_store.insert_alert(
        source="artifact", external_id=key, severity="medium", headline=headline,
        body=body, topic_tags=["artifact"], db_path=db["path"], **kw,
    )
    assert aid is not None
    return f"alert:{aid}"


def _run(db: dict[str, Any], run_id: str, name: str, title: str, body: str) -> str:
    wf_persistence.create_run(run_id, name, title, {}, db_path=db["path"])
    wf_persistence.complete_run(run_id, body, db_path=db["path"])
    return f"run:{run_id}"


async def _ids(q: str, archived: bool = False) -> set[str]:
    res = await artifacts_route.list_artifacts(_REQ, archived=archived, q=q)
    return {a.id for a in res["artifacts"]}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_matches_title_text_and_source(db: dict[str, Any]) -> None:
    memo = _draft(db, "a1", "Board memo", "We respond to the price cut.")
    plan = _draft(db, "a2", "Hiring plan", "Two engineers in Q1.")
    watch = _run(db, "r1", "weekly_fleet_pricing_watch", "Week of Sept 28", "Prices held.")

    assert await _ids("board") == {memo}
    assert await _ids("PRICE CUT") == {memo}  # any case, text
    assert await _ids("engineers q1") == {plan}  # every word, any order
    assert await _ids("fleet pricing") == {watch}  # workflow name as words
    assert await _ids("") == {memo, plan, watch}  # empty = everything
    assert await _ids("board engineers") == set()


async def test_search_finds_letters_beyond_a_to_z_as_typed(db: dict[str, Any]) -> None:
    uber = _draft(db, "a1", "Über plan", "Résumé review")
    assert await _ids("Über") == {uber}
    assert await _ids("Résumé") == {uber}


async def test_search_escapes_like_wildcards(db: dict[str, Any]) -> None:
    _draft(db, "a1", "Plain", "nothing special")
    pct = _draft(db, "a2", "Margin", "Up 40% this year")
    assert await _ids("%") == {pct}
    assert await _ids("_") == set()


async def test_search_stays_within_what_the_caller_sees(db: dict[str, Any]) -> None:
    theirs = _draft(db, "a1", "Secret memo", "private", owner_person_id=2)
    mine = _draft(db, "a2", "Secret plan", "mine")
    assert await _ids("secret") == {mine}
    db["viewer"] = TEAMMATE
    assert await _ids("secret") == {theirs}


async def test_search_respects_the_archive_view(db: dict[str, Any]) -> None:
    cid = _draft(db, "a1", "Old memo", "text")
    await artifacts_route.archive_artifact(cid, _REQ)
    assert await _ids("memo") == set()
    assert await _ids("memo", archived=True) == {cid}


# ---------------------------------------------------------------------------
# Share by link
# ---------------------------------------------------------------------------


def _key(path: str) -> str:
    assert path.startswith("/shared/")
    return path.removeprefix("/shared/")


async def test_share_open_copy_and_stop(db: dict[str, Any]) -> None:
    cid = _draft(db, "a1", "Board memo", "## Plan\n\nHold the price.")
    res = await artifacts_route.share_artifact(cid, _REQ)
    key = _key(res["share_path"])
    assert artifact_shares.KEY_PATTERN.fullmatch(key)
    # Asking again answers the same link (Copy).
    assert (await artifacts_route.share_artifact(cid, _REQ))["share_path"] == res["share_path"]

    listed = {a.id: a for a in (await artifacts_route.list_artifacts(_REQ))["artifacts"]}
    assert listed[cid].can_share is True
    assert listed[cid].share_path == res["share_path"]

    response = Response()
    doc = await artifacts_route.shared_document(key, response)
    assert doc.title == "Board memo"
    assert "Hold the price." in doc.body
    assert set(doc.model_dump()) == {"title", "format", "format_label", "created_at", "body"}
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["Cache-Control"] == "no-store"

    await artifacts_route.stop_sharing_artifact(cid, _REQ)
    with pytest.raises(HTTPException) as exc:
        await artifacts_route.shared_document(key, Response())
    assert exc.value.status_code == 404
    listed = {a.id: a for a in (await artifacts_route.list_artifacts(_REQ))["artifacts"]}
    assert listed[cid].share_path is None


async def test_two_shares_at_once_make_one_link(db: dict[str, Any]) -> None:
    import asyncio

    cid = _draft(db, "a1", "Board memo", "text")
    keys = await asyncio.gather(*(
        asyncio.to_thread(artifact_shares.share, cid, viewer=PRINCIPAL) for _ in range(8)
    ))
    assert len(set(keys)) == 1


@pytest.mark.parametrize("bad", ["", "short", "x" * 31 + "!", "../../etc/passwd" + "a" * 16])
async def test_malformed_or_unknown_keys_are_not_found(db: dict[str, Any], bad: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await artifacts_route.shared_document(bad, Response())
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document("a" * 32, Response())


async def test_archive_pauses_the_link_and_delete_ends_it(db: dict[str, Any]) -> None:
    cid = _draft(db, "a1", "Memo", "text")
    key = _key((await artifacts_route.share_artifact(cid, _REQ))["share_path"])
    await artifacts_route.archive_artifact(cid, _REQ)
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document(key, Response())
    await artifacts_route.restore_artifact(cid, _REQ)
    assert (await artifacts_route.shared_document(key, Response())).title == "Memo"
    await artifacts_route.delete_artifact(cid, _REQ)
    assert artifact_shares.share_keys([cid]) == {}


async def test_archived_document_cannot_be_shared(db: dict[str, Any]) -> None:
    cid = _draft(db, "a1", "Memo", "text")
    await artifacts_route.archive_artifact(cid, _REQ)
    with pytest.raises(HTTPException) as exc:
        await artifacts_route.share_artifact(cid, _REQ)
    assert exc.value.status_code == 409


async def test_link_documents_keep_their_own_sharing(db: dict[str, Any]) -> None:
    cid = _draft(
        db, "a1", "Q4 plan", "A doc", artifact_format="link",
        artifact_url="https://docs.example.com/d/1",
    )
    listed = {a.id: a for a in (await artifacts_route.list_artifacts(_REQ))["artifacts"]}
    assert listed[cid].can_share is False
    with pytest.raises(HTTPException) as exc:
        await artifacts_route.share_artifact(cid, _REQ)
    assert exc.value.status_code == 409


async def test_who_may_share(db: dict[str, Any]) -> None:
    team_run = _run(db, "r1", "board_prep", "Deck", "# Deck")
    teammates = _draft(db, "a1", "Their memo", "text", owner_person_id=2)

    # A teammate can open the team's run but not share it.
    db["viewer"] = TEAMMATE
    listed = {a.id: a for a in (await artifacts_route.list_artifacts(_REQ))["artifacts"]}
    assert listed[team_run].can_share is False
    with pytest.raises(HTTPException) as exc:
        await artifacts_route.share_artifact(team_run, _REQ)
    assert exc.value.status_code == 404
    # Their own draft, they can.
    assert listed[teammates].can_share is True
    await artifacts_route.share_artifact(teammates, _REQ)

    # Someone else can't see it, so can't share or stop it.
    db["viewer"] = OTHER
    for call in (artifacts_route.share_artifact, artifacts_route.stop_sharing_artifact):
        with pytest.raises(HTTPException) as exc:
            await call(teammates, _REQ)
        assert exc.value.status_code == 404
    assert artifact_shares.share_keys([teammates]) != {}

    # The principal shares the team's run.
    db["viewer"] = PRINCIPAL
    await artifacts_route.share_artifact(team_run, _REQ)
    # A teammate who can open it never sees its link.
    db["viewer"] = TEAMMATE
    listed = {a.id: a for a in (await artifacts_route.list_artifacts(_REQ))["artifacts"]}
    assert listed[team_run].can_share is False and listed[team_run].share_path is None


async def test_a_malformed_id_is_a_bad_request(db: dict[str, Any]) -> None:
    for call in (artifacts_route.share_artifact, artifacts_route.stop_sharing_artifact):
        with pytest.raises(HTTPException) as exc:
            await call("not-an-id", _REQ)
        assert exc.value.status_code == 400


async def test_link_ends_when_its_sharer_leaves(db: dict[str, Any]) -> None:
    db["viewer"] = TEAMMATE
    cid = _draft(db, "a1", "Their memo", "text", owner_person_id=2)
    key = _key((await artifacts_route.share_artifact(cid, _REQ))["share_path"])
    assert (await artifacts_route.shared_document(key, Response())).title == "Their memo"
    db["left"].add(2)
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document(key, Response())
    # Ended, not paused: coming back doesn't bring the old link back.
    db["left"].discard(2)
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document(key, Response())
    assert artifact_shares.share_keys([cid]) == {}
    # Sharing again makes a new link.
    again = _key((await artifacts_route.share_artifact(cid, _REQ))["share_path"])
    assert again != key


async def test_a_busy_people_list_doesnt_end_a_link(db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    cid = _draft(db, "a1", "Memo", "text")
    key = _key((await artifacts_route.share_artifact(cid, _REQ))["share_path"])

    def locked(pid: int) -> Any:
        raise sqlite3.OperationalError("database is locked")

    from openexecutive.people import store as people_store

    on_roster = people_store.get_person
    monkeypatch.setattr(people_store, "get_person", locked)
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document(key, Response())
    assert artifact_shares.share_keys([cid]) == {cid: key}
    monkeypatch.setattr(people_store, "get_person", on_roster)
    assert (await artifacts_route.shared_document(key, Response())).title == "Memo"


async def test_a_new_principal_gets_a_new_link(db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(db, "r1", "board_prep", "Deck", "# Deck")
    old = _key((await artifacts_route.share_artifact(run, _REQ))["share_path"])
    successor = Viewer(person_id=3, is_principal=True)
    db["viewer"] = successor
    new = _key((await artifacts_route.share_artifact(run, _REQ))["share_path"])
    assert new != old
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document(old, Response())


async def test_share_and_stop_are_audited(db: dict[str, Any]) -> None:
    db["viewer"] = TEAMMATE
    cid = _draft(db, "a1", "Their memo", "text", owner_person_id=2)
    await artifacts_route.share_artifact(cid, _REQ)
    await artifacts_route.share_artifact(cid, _REQ)  # asking again is not a new share
    await artifacts_route.stop_sharing_artifact(cid, _REQ)
    await artifacts_route.stop_sharing_artifact(cid, _REQ)  # nothing left to stop
    events = [(e, kw["details"]["artifact_id"], kw["private_to_person"]) for e, kw in db["audited"]]
    assert events == [("document_shared", cid, 2), ("document_share_stopped", cid, 2)]


async def test_a_revision_is_not_on_the_old_link(db: dict[str, Any]) -> None:
    first = _draft(db, "a1", "Memo v1", "first text")
    key = _key((await artifacts_route.share_artifact(first, _REQ))["share_path"])
    _draft(db, "a2", "Memo v2", "second text", supersedes_id=first)
    doc = await artifacts_route.shared_document(key, Response())
    assert "first text" in doc.body


def test_record_loader_is_unchanged_for_viewers(db: dict[str, Any]) -> None:
    cid = _draft(db, "a1", "Memo", "text", owner_person_id=2)
    with pytest.raises(artifact_records.ArtifactNotFound):
        artifact_records.load_artifact(cid, viewer=PRINCIPAL)


async def test_reading_links_never_writes(db: dict[str, Any]) -> None:
    import sqlite3

    _draft(db, "a1", "Memo", "text")
    await artifacts_route.list_artifacts(_REQ)
    with pytest.raises(HTTPException):
        await artifacts_route.shared_document("a" * 32, Response())
    assert artifact_shares.forget("alert:1") is False
    with sqlite3.connect(db["path"]) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "artifact_shares" not in tables
