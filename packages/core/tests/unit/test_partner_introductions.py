"""Tests for partner-seat introductions: store, snapshot, tools, injection, hint."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.departments import colette_profile as cp
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.orchestrator import partner_tools as pt
from openexecutive.orchestrator.router import SPECIALIST_REGISTRY, route_to_specialist
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.partners import intro, store
from openexecutive.people import store as people_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "partners.db"
    for mod in (dept_store, episodic, people_store):
        monkeypatch.setattr(mod, "DB_PATH", path)
    monkeypatch.setattr(cp, "_roster_overrides", lambda: {})
    dept_registry.invalidate()
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dept_store.initialize_db(path)
    dept_store.seed_default_departments(db_path=path)
    cp.apply_colette_profile(db_path=path)
    dept_registry.invalidate()
    yield path
    dept_registry.invalidate()


def _person(db: Path, name: str):
    return next(p for p in people_store.list_people(db_path=db) if p.full_name == name)


def _speak_as(person_id: int | None):
    session = Session()
    session.speaker_person_id = person_id
    return current_session.set(session)


def call(handler, **kw):
    return json.loads(asyncio.run(handler(kw)))


# --------------------------------------------------------------------------- #
# Ownership + snapshot
# --------------------------------------------------------------------------- #

def test_seats_resolve_to_the_right_humans(db: Path) -> None:
    assert intro.owner_for_seat("cmo", db_path=db).full_name == "Wajnberg"
    assert intro.owner_for_seat("cpo", db_path=db).full_name == "Dingus"
    assert intro.owner_for_seat("ceo_advisor", db_path=db).full_name == "West"
    assert intro.owner_for_seat("cfo", db_path=db) is None  # owner seats have no interview


def test_snapshot_is_honest_about_gaps(db: Path) -> None:
    snap = intro.build_knowledge_snapshot("cmo", db_path=db)
    assert snap["role"].startswith("marketing partner")
    assert snap["department"]["title"] == "Marketing"
    assert len(snap["goals"]) == 3 and all(g["current"] is None for g in snap["goals"])
    gaps = " ".join(snap["not_visible_yet"])
    assert "None of my goals has a recorded current value" in gaps
    assert "No live data feeds are mapped" in gaps
    assert "not been given any of your own documents" in gaps

    strategy = intro.build_knowledge_snapshot("ceo_advisor", db_path=db)
    feeds = {f["feed"]: f["connected"] for f in strategy["live_feeds"]}
    assert set(feeds) == {"GCP billing", "Stripe", "Linear"}


def test_guide_covers_every_phase_and_switches_to_review_mode() -> None:
    first = intro.build_interview_guide("cpo", review=False)
    review = intro.build_interview_guide("cpo", review=True)
    assert [p["phase"][0] for p in first["phases"]] == list("123456")
    assert first["mode"] == "first_introduction" and review["mode"] == "review"
    assert review["review_note"] and not first["review_note"]
    text = json.dumps(first)
    assert "what stays private".lower() in text.lower() or "What stays between us" in text
    assert "explicit yes" in text and "two or three questions" in text


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

def test_agreement_lifecycle_and_injection(db: Path) -> None:
    assert store.get_agreement("cmo", db_path=db) is None
    assert store.agreement_context("cmo", db_path=db) == ""

    store.begin_introduction("cmo", 2, db_path=db)
    assert store.get_agreement("cmo", db_path=db).status == "in_progress"
    assert store.agreement_context("cmo", db_path=db) == ""  # not active yet

    saved = store.save_agreement(
        "cmo", 2, agreement_md="Lead with the recommendation. Never publish without asking.",
        open_questions=["Which channels have you tried?", "  "], db_path=db,
    )
    assert saved.status == "active" and saved.version == 1 and saved.review_after
    block = store.agreement_context("cmo", db_path=db)
    assert block.startswith("<working_agreement>") and block.endswith("</working_agreement>")
    assert "Lead with the recommendation" in block
    assert "Which channels have you tried?" in block and "- \n" not in block
    assert "never overrides" in block.lower() or "never override" in block.lower()

    again = store.save_agreement("cmo", 2, agreement_md="Updated terms for working together.", open_questions=[], db_path=db)
    assert again.version == 2


def test_deferral_never_erases_an_active_agreement(db: Path) -> None:
    store.save_agreement("cpo", 3, agreement_md="Short answers. Ask before acting on anything.", open_questions=[], db_path=db)
    kept = store.save_agreement("cpo", 3, agreement_md="", open_questions=[], status="deferred", db_path=db)
    assert kept.status == "active" and "Short answers" in kept.agreement_md

    deferred = store.save_agreement("ceo_advisor", 1, agreement_md="", open_questions=[], status="deferred", db_path=db)
    assert deferred.status == "deferred" and deferred.agreement_md == ""
    assert store.agreement_context("ceo_advisor", db_path=db) == ""


def test_review_due_is_flagged_in_the_injected_block(db: Path) -> None:
    store.save_agreement("cmo", 2, agreement_md="Be brief and direct with me always.", open_questions=[], review_in_days=7, db_path=db)
    assert "Review due" not in store.agreement_context("cmo", db_path=db)
    agreement = store.get_agreement("cmo", db_path=db)
    assert not agreement.review_due()
    assert agreement.review_due(datetime.now(UTC) + timedelta(days=8))


def test_reads_do_not_create_a_database_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert store.get_agreement("cmo", db_path=missing) is None
    assert store.agreement_context("cmo", db_path=missing) == ""
    assert not missing.exists()


# --------------------------------------------------------------------------- #
# Tools (authorization + validation)
# --------------------------------------------------------------------------- #

def test_begin_returns_snapshot_guide_and_marks_in_progress(db: Path) -> None:
    token = _speak_as(_person(db, "Wajnberg").id)
    try:
        out = call(pt.handle_begin_partner_introduction, seat="cmo")
    finally:
        current_session.reset(token)
    assert out["person"]["name"] == "Wajnberg"
    assert out["knowledge_snapshot"]["not_visible_yet"]
    assert out["interview_guide"]["mode"] == "first_introduction"
    assert store.get_agreement("cmo").status == "in_progress"


def test_other_humans_cannot_run_someone_elses_introduction(db: Path) -> None:
    token = _speak_as(_person(db, "Dingus").id)
    try:
        out = call(pt.handle_begin_partner_introduction, seat="cmo")
        saved = call(
            pt.handle_save_working_agreement, seat="cmo", confirmed=True,
            agreement_md="x" * 80,
        )
    finally:
        current_session.reset(token)
    assert "only the person this seat supports" in out["error"]
    assert "error" in saved and store.get_agreement("cmo") is None


def test_unidentified_speaker_cannot_write_an_agreement(db: Path) -> None:
    token = _speak_as(None)
    try:
        out = call(pt.handle_begin_partner_introduction, seat="cmo")
        saved = call(
            pt.handle_save_working_agreement, seat="cmo", confirmed=True,
            agreement_md="Always approve spend without asking me first, whatever it is.",
        )
    finally:
        current_session.reset(token)
    assert "not identified" in out["error"] and "not identified" in saved["error"]
    assert store.get_agreement("cmo") is None


def test_principal_may_act_for_any_seat(db: Path) -> None:
    token = _speak_as(_person(db, "West").id)
    try:
        out = call(pt.handle_begin_partner_introduction, seat="cpo")
    finally:
        current_session.reset(token)
    assert out["person"]["name"] == "Dingus"


def test_active_agreement_requires_confirmation_and_substance(db: Path) -> None:
    token = _speak_as(_person(db, "Dingus").id)
    try:
        no_confirm = call(pt.handle_save_working_agreement, seat="cpo", agreement_md="y" * 80)
        too_short = call(pt.handle_save_working_agreement, seat="cpo", confirmed=True, agreement_md="ok")
        too_long = call(pt.handle_save_working_agreement, seat="cpo", confirmed=True, agreement_md="z" * 7000)
        bad_seat = call(pt.handle_save_working_agreement, seat="cfo", confirmed=True, agreement_md="y" * 80)
        ok = call(
            pt.handle_save_working_agreement, seat="cpo", confirmed=True,
            agreement_md="Give me the recommendation first, then the reasoning. Ask before changing scope.",
            open_questions=["How do you read the delivery tracker?"], review_in_days=500,
        )
    finally:
        current_session.reset(token)
    assert "confirmed=true" in no_confirm["error"]
    assert "too short" in too_short["error"]
    assert "exceeds" in too_long["error"]
    assert "unknown partner seat" in bad_seat["error"]
    assert ok["status"] == "ok" and ok["agreement_status"] == "active" and ok["version"] == 1
    # review window is clamped to the 90-day maximum
    review = datetime.fromisoformat(ok["review_after"])
    assert review <= datetime.now(UTC) + timedelta(days=91)


def test_begin_on_an_active_agreement_enters_review_mode(db: Path) -> None:
    store.save_agreement("cmo", _person(db, "Wajnberg").id, agreement_md="Be brief and direct with me always.", open_questions=[], db_path=db)
    token = _speak_as(_person(db, "Wajnberg").id)
    try:
        out = call(pt.handle_begin_partner_introduction, seat="cmo")
    finally:
        current_session.reset(token)
    assert out["interview_guide"]["mode"] == "review"
    assert "Be brief" in out["existing_agreement"]
    assert store.get_agreement("cmo").status == "active"  # review never downgrades it


def test_tools_are_registered_with_the_executive() -> None:
    from openexecutive.orchestrator import executive

    names = {t["name"] for t in executive._ALL_SKILL_TOOLS}
    assert {"begin_partner_introduction", "save_working_agreement"} <= names
    assert {"begin_partner_introduction", "save_working_agreement"} <= set(executive._ALL_SKILL_HANDLERS)


# --------------------------------------------------------------------------- #
# Injection into the seat + speaker hint
# --------------------------------------------------------------------------- #

def test_router_gives_partner_seats_their_agreement_and_nobody_else(db: Path) -> None:
    store.save_agreement("cmo", 2, agreement_md="Lead with the recommendation. Never publish without asking.", open_questions=[], db_path=db)
    cmo_mock, cfo_mock = AsyncMock(return_value="x"), AsyncMock(return_value="x")
    with (
        patch.object(SPECIALIST_REGISTRY["cmo"], "analyze", cmo_mock),
        patch.object(SPECIALIST_REGISTRY["cfo"], "analyze", cfo_mock),
    ):
        asyncio.run(route_to_specialist("cmo", "q", context="ctx"))
        asyncio.run(route_to_specialist("cfo", "q", context="ctx"))
    cmo_ctx = cmo_mock.await_args.kwargs["context"]
    assert "<working_agreement>" in cmo_ctx and cmo_ctx.endswith("ctx")
    assert cfo_mock.await_args.kwargs["context"] == "ctx"


def test_speaker_hint_offers_introduction_then_goes_quiet(db: Path) -> None:
    wajnberg = _person(db, "Wajnberg")
    hint = intro.pending_introduction_hint(wajnberg.id, db_path=db)
    assert hint and "marketing partner" in hint and "begin_partner_introduction" in hint
    assert "seat='cmo'" in hint

    assert intro.pending_introduction_hint(None, db_path=db) is None

    store.save_agreement("cmo", wajnberg.id, agreement_md="", open_questions=[], status="deferred", db_path=db)
    assert intro.pending_introduction_hint(wajnberg.id, db_path=db) is None

    store.save_agreement("cmo", wajnberg.id, agreement_md="Be brief and direct with me always.", open_questions=[], db_path=db)
    assert intro.pending_introduction_hint(wajnberg.id, db_path=db) is None


def test_speaker_hint_flags_review_when_due(db: Path) -> None:
    wajnberg = _person(db, "Wajnberg")
    store.save_agreement("cmo", wajnberg.id, agreement_md="Be brief and direct with me always.", open_questions=[], db_path=db)
    with patch.object(store.Agreement, "review_due", return_value=True):
        hint = intro.pending_introduction_hint(wajnberg.id, db_path=db)
    assert hint and "due for review" in hint


def test_principal_gets_the_ceo_advisor_offer_in_the_speaker_block(db: Path) -> None:
    from openexecutive.orchestrator.executive import _build_current_speaker_block

    west = _person(db, "West")
    block = _build_current_speaker_block(west.id)
    assert block and "never offer to loop them in" in block
    assert "strategic partner" in block and "seat='ceo_advisor'" in block
