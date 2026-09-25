"""Where the daily brief went, and telling the owner when it went nowhere.

  * Each brief's latest send is recorded: the reason, and the channel that
    sent it (a name, never an address).
  * The Setup status page's "Daily brief" light says when the briefs go out
    and where, and turns amber or red when they can't.
  * ``GET /today/brief-delivery`` gives the owner, and only the owner, the
    latest brief that wasn't sent, until its cause is fixed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.setup_checks import Snapshot, check_brief
from openexecutive.briefing import brief_state, narrative_cache
from openexecutive.briefing.brief_state import DeliveryOutcome
from openexecutive.config import Settings
from openexecutive.people.models import Person

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
MORNING = "principal_brief_morning"
EVENING = "principal_brief_eod"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_the_latest_send_of_either_brief_is_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert brief_state.last_delivery_outcome() is None
    brief_state.record_delivery_outcome(MORNING, reason="delivered", channel="email")
    later = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(narrative_cache, "utc_now_iso", lambda: later.isoformat())
    brief_state.record_delivery_outcome(EVENING, reason="no_channel", channel=None)

    latest = brief_state.last_delivery_outcome()
    assert latest is not None
    assert (latest.kind, latest.reason, latest.channel) == (EVENING, "no_channel", None)


def test_an_unreadable_record_is_ignored() -> None:
    narrative_cache.put(narrative_cache.BriefingNarrative(
        scope=f"{brief_state.DELIVERY_SCOPE_PREFIX}{MORNING}",
        input_hash="made-up-reason",
        narrative_text="",
        generated_at=narrative_cache.utc_now_iso(),
    ))
    assert brief_state.last_delivery_outcome() is None


@pytest.mark.parametrize(
    ("reason", "can_deliver", "tell"),
    [
        ("delivered", False, False),
        ("send_failed", True, True),  # a channel exists but sending failed
        ("no_channel", False, True),  # still nowhere to send it
        ("no_channel", True, False),  # fixed since: the next one will go
        ("no_owner", True, False),
    ],
)
def test_still_unsent(reason: str, can_deliver: bool, tell: bool) -> None:
    outcome = DeliveryOutcome(MORNING, reason, None, NOW)  # type: ignore[arg-type]
    assert brief_state.still_unsent(outcome, can_deliver=can_deliver) is tell
    assert brief_state.still_unsent(None, can_deliver=can_deliver) is False


# ---------------------------------------------------------------------------
# The "Daily brief" light
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-realistic",
        "EXEC_EMAIL_ADDRESS": "exec@acme.io",
        "SCHEDULER_ENABLED": True,
    }
    return Settings(**{**base, **overrides})


def _snap(**fields: Any) -> Snapshot:
    values: dict[str, Any] = {
        "settings": _settings(),
        "now": NOW,
        "local_login": False,
        "people": [],
        "principal": Person(id=3, full_name="Ada", is_principal=True, email="ada@acme.io"),
        "last_inbound": {},
        "mcp_gateway": object(),  # Gmail connected
        "brief_next_runs": {
            MORNING: datetime(2026, 9, 26, 15, 0, tzinfo=UTC),  # 08:00 in Los Angeles
            EVENING: datetime(2026, 9, 26, 1, 0, tzinfo=UTC),  # 18:00 in Los Angeles
        },
        "brief_zone": "America/Los_Angeles",
    }
    values.update(fields)
    return Snapshot(**values)


def test_the_light_says_when_and_where() -> None:
    check = check_brief(_snap())
    assert check.state == "ok"
    assert check.summary == (
        "Sent to you by email: the morning brief at 08:00 and the end-of-day digest at 18:00 "
        "(America/Los_Angeles)."
    )


def test_a_chat_preference_is_named() -> None:
    ada = Person(id=3, full_name="Ada", is_principal=True, preferred_channel="slack", slack_user_id="U1")
    assert check_brief(_snap(principal=ada)).summary.startswith("Sent to you on Slack")


def test_no_time_zone_means_utc_and_says_so() -> None:
    check = check_brief(_snap(brief_zone=None))
    assert check.state == "warn"
    assert check.summary == (
        "Sent to you by email: the morning brief at 15:00 and the end-of-day digest at 01:00, "
        "in UTC because no time zone is set."
    )
    assert (check.fix, check.link) == ("Set your time zone in Settings.", "/settings")


def test_nowhere_to_send_it() -> None:
    check = check_brief(_snap(mcp_gateway=None))  # Gmail off, no chat ids
    assert check.state == "warn"
    assert check.summary == "Kept in the app only: nothing is set up to send it to you."
    assert check.link == "/people/3"


def test_no_owner() -> None:
    check = check_brief(_snap(principal=None))
    assert (check.state, check.link) == ("warn", "/people")


def test_a_failed_send_turns_it_red() -> None:
    failed = DeliveryOutcome(EVENING, "send_failed", None, NOW)
    check = check_brief(_snap(brief_delivery=failed))
    assert check.state == "error"
    assert check.summary == "Your last end-of-day digest wasn't sent: every way of sending it failed."


def test_a_fixed_cause_is_not_reported() -> None:
    # It had nowhere to go, and now it has (email is connected in this snapshot).
    stale = DeliveryOutcome(MORNING, "no_channel", None, NOW)
    assert check_brief(_snap(brief_delivery=stale)).state == "ok"


def test_off_with_the_scheduler() -> None:
    check = check_brief(_snap(settings=_settings(SCHEDULER_ENABLED=False)))
    assert check.state == "off"


# ---------------------------------------------------------------------------
# GET /today/brief-delivery
# ---------------------------------------------------------------------------


@pytest.fixture
def notice_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.api.routes import today as today_route
    from openexecutive.scheduler import runner

    state: dict[str, Any] = {"owner": True, "plan": []}
    monkeypatch.setattr(chat_route, "_caller_is_principal_or_unclaimed", lambda _r: state["owner"])
    monkeypatch.setattr(runner, "_principal_delivery_plan", lambda: (None, state["plan"]))
    app = FastAPI()
    app.include_router(today_route.router)
    return TestClient(app), state


def test_the_owner_hears_about_an_unsent_brief(notice_client: Any) -> None:
    client, _ = notice_client
    brief_state.record_delivery_outcome(MORNING, reason="no_channel", channel=None)
    body = client.get("/today/brief-delivery").json()
    assert body["brief"] == "morning brief"
    assert body["problem"] == "nothing is set up to send it to you"
    assert body["fix"].startswith("Connect Gmail")
    assert "@" not in str(body)


def test_nobody_else_does(notice_client: Any) -> None:
    client, state = notice_client
    state["owner"] = False
    brief_state.record_delivery_outcome(MORNING, reason="send_failed", channel=None)
    assert client.get("/today/brief-delivery").json() is None


@pytest.mark.parametrize(
    ("reason", "plan"),
    [
        ("delivered", ["email"]),
        ("no_channel", ["email"]),  # fixed since
    ],
)
def test_nothing_to_say(notice_client: Any, reason: str, plan: list[str]) -> None:
    client, state = notice_client
    state["plan"] = plan
    brief_state.record_delivery_outcome(MORNING, reason=reason, channel="email")  # type: ignore[arg-type]
    assert client.get("/today/brief-delivery").json() is None


def test_nothing_recorded_yet(notice_client: Any) -> None:
    client, _ = notice_client
    assert client.get("/today/brief-delivery").json() is None
