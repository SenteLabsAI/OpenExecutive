"""Act as me: who may have it, who has it on, and the per-turn offer
(delegation/settings.py)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

# Imported up front: api.main builds the app at import time, which must not
# happen under a test's patched OE_PUBLIC_DEPLOYMENT.
from openexecutive.api import main as api_main
from openexecutive.delegation import settings as dsettings
from openexecutive.delegation.settings import (
    DelegationOverride,
    TurnDelegation,
    block0_delegation_on,
    can_delegate,
    enabled_for_install,
    is_enabled,
    pin_turn_delegation,
    set_enabled,
    turn_touched_delegate_mail,
)
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import Person


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    prior = current_session.get()
    current_session.set(None)
    yield path
    current_session.set(prior)
    people_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email="olivia@co.example",
        slack_user_id="U_OWNER", discord_user_id="1001", telegram_chat_id="5001",
    )
    teammate = people_store.upsert_person(
        full_name="Ben Teammate", email="ben@co.example", slack_user_id="U_BEN",
    )
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


def _person(**kw: object) -> Person:
    return Person(**{"id": 1, "full_name": "X", **kw})  # type: ignore[arg-type]


def test_only_the_principal_may_have_it() -> None:
    assert can_delegate(_person(is_principal=True)) is True
    assert can_delegate(_person()) is False
    assert can_delegate(_person(is_principal=True, archived=True)) is False
    assert can_delegate(_person(is_principal=True, id=None)) is False
    assert can_delegate(None) is False


def test_off_until_turned_on(roster: SimpleNamespace, db: Path) -> None:
    assert is_enabled(roster.principal) is False
    set_enabled(roster.principal, True, updated_by="test")
    assert is_enabled(roster.principal) is True
    set_enabled(roster.principal, False, updated_by="test")
    assert is_enabled(roster.principal) is False


def test_a_missing_file_or_table_reads_as_off(tmp_path: Path) -> None:
    assert is_enabled(1, db_path=tmp_path / "nope.db") is False
    empty = tmp_path / "empty.db"
    empty.touch()
    assert is_enabled(1, db_path=empty) is False
    assert is_enabled(None) is False


def test_the_install_flag_is_the_principals_setting(roster: SimpleNamespace) -> None:
    # A teammate's row (which the route never writes) does not count.
    set_enabled(roster.teammate, True, updated_by="test")
    assert enabled_for_install() is False
    set_enabled(roster.principal, True, updated_by="test")
    assert enabled_for_install() is True


def test_block0_follows_an_eval_override(roster: SimpleNamespace) -> None:
    set_enabled(roster.principal, True, updated_by="test")
    assert block0_delegation_on(Session()) is True
    assert block0_delegation_on(Session(delegation_override=DelegationOverride(enabled=False))) is False


# --------------------------------------------------------------------------- #
# The offer
# --------------------------------------------------------------------------- #


def _web(person_id: int, *, signed_in: bool = True) -> Session:
    return Session(from_web_chat=True, caller_person_id=person_id, web_caller_signed_in=signed_in)


@pytest.mark.parametrize(("name", "make", "offered"), [
    ("principal on the web, signed in", lambda r: _web(r.principal), True),
    ("principal on the web, no sign-in", lambda r: _web(r.principal, signed_in=False), False),
    ("principal on their own Slack",
     lambda r: Session(origin_channel="slack", origin_channel_ref="D1", caller_person_id=r.principal), True),
    ("principal on their own Discord",
     lambda r: Session(origin_channel="discord", origin_channel_ref="D1", caller_person_id=r.principal), True),
    ("principal on Telegram without a webhook secret",
     lambda r: Session(origin_channel="telegram", origin_channel_ref="5001", caller_person_id=r.principal), False),
    ("a teammate on the web", lambda r: _web(r.teammate), False),
    ("an email turn about the principal's mail",
     lambda r: Session(caller_person_id=r.principal, private_to_principal=True), False),
    ("an unattended run", lambda r: Session(caller_person_id=r.principal, unattended=True), False),
    ("the MCP server (a bare session)", lambda r: Session(caller_person_id=r.principal), False),
    ("an unresolved caller", lambda r: _web(0), False),
])
def test_who_is_offered_it(roster: SimpleNamespace, name: str, make, offered: bool) -> None:
    set_enabled(roster.principal, True, updated_by="test")
    # A teammate with a row of their own is still refused (can_delegate).
    set_enabled(roster.teammate, True, updated_by="test")
    session = make(roster)
    pinned = pin_turn_delegation(session, "hi")
    assert pinned.offered is offered, name
    assert session.turn_delegation is pinned


def test_not_offered_while_it_is_off(roster: SimpleNamespace) -> None:
    assert pin_turn_delegation(_web(roster.principal), "hi").offered is False


def test_local_login_stands_in_for_a_web_sign_in(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_enabled(roster.principal, True, updated_by="test")
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert pin_turn_delegation(_web(roster.principal, signed_in=False), "hi").offered is True
    # Never on a public deployment.
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    assert pin_turn_delegation(_web(roster.principal, signed_in=False), "hi").offered is False


@pytest.mark.parametrize(("override", "offered"), [
    (DelegationOverride(enabled=True, gmail=object()), True),
    (DelegationOverride(enabled=True, gmail=None), False),
    (DelegationOverride(enabled=False, gmail=object()), False),
])
def test_an_eval_override_needs_a_fake_mailbox(override: DelegationOverride, offered: bool) -> None:
    session = Session(delegation_override=override)
    assert pin_turn_delegation(session, "hi").offered is offered


def test_an_override_never_widens_an_unattended_or_private_turn() -> None:
    override = DelegationOverride(enabled=True, gmail=object())
    assert pin_turn_delegation(Session(delegation_override=override, unattended=True), "x").offered is False
    assert pin_turn_delegation(
        Session(delegation_override=override, private_to_principal=True), "x"
    ).offered is False


def test_each_turn_starts_untouched(roster: SimpleNamespace) -> None:
    set_enabled(roster.principal, True, updated_by="test")
    session = _web(roster.principal)
    pin_turn_delegation(session, "first").touched_mail = True
    assert turn_touched_delegate_mail(session) is True
    pinned = pin_turn_delegation(session, "second")
    assert pinned.touched_mail is False
    assert pinned.speaker_text == "second"
    assert turn_touched_delegate_mail(session) is False


def test_touched_reads_the_bound_session() -> None:
    session = Session(turn_delegation=TurnDelegation(touched_mail=True))
    token = current_session.set(session)
    try:
        assert turn_touched_delegate_mail() is True
    finally:
        current_session.reset(token)
    assert turn_touched_delegate_mail() is False


def test_a_lookup_failure_leaves_it_off(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    set_enabled(roster.principal, True, updated_by="test")

    def boom(_person_id: int) -> None:
        raise RuntimeError("roster down")

    monkeypatch.setattr("openexecutive.people.store.get_person", boom)
    pinned = pin_turn_delegation(_web(roster.principal), "hi")
    assert (pinned.enabled, pinned.offered) == (False, False)


@pytest.mark.parametrize(("local", "public"), [
    (None, None), ("1", None), ("1", "1"), ("1", "false"), ("0", None), ("true", None),
])
def test_local_login_matches_the_api(
    monkeypatch: pytest.MonkeyPatch, local: str | None, public: str | None
) -> None:
    for var, value in (("OE_LOCAL_LOGIN", local), ("OE_PUBLIC_DEPLOYMENT", public)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    assert dsettings.local_login() is api_main._is_local_login()


def test_its_tables_are_per_company() -> None:
    from openexecutive.clients.slots import _BLANK_WIPE_TABLES
    from openexecutive.delegation.schema import TABLES

    assert set(TABLES) <= set(_BLANK_WIPE_TABLES)


# --------------------------------------------------------------------------- #
# The setup status check
# --------------------------------------------------------------------------- #


def _check(principal: Person | None, status: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    import asyncio

    from openexecutive.api import setup_checks

    async def fake_status(email: str | None, *, gmail: object = None) -> str:
        return status

    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_status", fake_status)
    result = asyncio.run(setup_checks.check_your_gmail(SimpleNamespace(principal=principal)))  # type: ignore[arg-type]
    return result.state, result.summary


def test_the_setup_check(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = people_store.get_person(roster.principal)
    assert owner is not None
    assert _check(None, "connected", monkeypatch)[0] == "off"
    assert _check(owner, "not_configured", monkeypatch)[0] == "off"
    state, summary = _check(owner, "connected", monkeypatch)
    assert state == "ok" and "Turn Act as me on" in summary
    set_enabled(roster.principal, True, updated_by="test")
    assert _check(owner, "connected", monkeypatch)[1].endswith("in your own Gmail.")
    assert _check(owner, "not_configured", monkeypatch)[0] == "warn"
    assert _check(owner, "needs_reconnect", monkeypatch)[0] == "error"


def test_the_setup_check_is_bounded(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from openexecutive.api import setup_checks

    async def slow_status(email: str | None, *, gmail: object = None) -> str:
        await asyncio.sleep(10)
        return "connected"

    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_status", slow_status)
    monkeypatch.setattr(setup_checks, "PROBE_TIMEOUT_S", 0.05)
    owner = people_store.get_person(roster.principal)
    result = asyncio.run(setup_checks.check_your_gmail(SimpleNamespace(principal=owner)))  # type: ignore[arg-type]
    assert result.state == "warn" and "didn't answer" in result.summary
