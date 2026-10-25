"""The first week's GETTING STARTED block (briefing/first_week.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from openexecutive.briefing import first_week
from openexecutive.departments.models import (
    DepartmentCharter,
    DepartmentConfig,
    DepartmentState,
    Goal,
)

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def _principal(created: datetime | None, *, email: str = "owner@acme.test") -> SimpleNamespace:
    return SimpleNamespace(id=1, email=email, created_at=created.isoformat() if created else "")


def _dept(slug: str, title: str, *, head: int | None, goals: list[Goal] | None = None) -> DepartmentState:
    return DepartmentState(
        config=DepartmentConfig(
            slug=slug, title=title, charter=DepartmentCharter(mission=""), head_person_id=head,
        ),
        goals=goals or [],
    )


def _goal(goal_id: int, slug: str, key_result: str, target: str = "") -> Goal:
    return Goal(id=goal_id, department_slug=slug, period_value="Q4 2026", key_result=key_result, target=target)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        principal=_principal(NOW - timedelta(days=1)),
        departments=[
            _dept("sales", "Sales", head=None, goals=[_goal(7, "sales", "Close 10 new accounts", "10")]),
            _dept("ops", "Operations", head=2),
        ],
    )
    monkeypatch.setattr("openexecutive.people.store.find_principal_person", lambda *a, **k: state.principal)
    state.loose_goals = []
    monkeypatch.setattr("openexecutive.departments.store.list_departments", lambda *a, **k: state.departments)
    monkeypatch.setattr(
        "openexecutive.departments.store.list_all_goals",
        lambda *a, **k: [g for d in state.departments for g in d.goals] + state.loose_goals,
    )
    return state


def test_first_week_runs_seven_days_from_the_principals_row(setup: SimpleNamespace) -> None:
    assert first_week.first_week_started(NOW) is not None
    setup.principal = _principal(NOW - timedelta(days=8))
    assert first_week.first_week_started(NOW) is None
    setup.principal = _principal(None)
    assert first_week.first_week_started(NOW) is None
    setup.principal = None
    assert first_week.first_week_started(NOW) is None


def test_shared_block_lists_goals_and_departments_with_no_leader(setup: SimpleNamespace) -> None:
    block = first_week.getting_started_shared(NOW)
    assert block.text.startswith(first_week.HEADER)
    assert "- [Sales] Close 10 new accounts — target 10 (Q4 2026)" in block.text
    assert "DEPARTMENTS WITH NO LEADER" in block.text
    assert "- Sales" in block.text
    assert "Operations" not in block.text  # it has a leader and no goals
    assert "INBOX" not in block.text
    assert block.private is False
    assert sorted(block.keys) == ["goal:7:on_track", "leaderless:sales"]


def test_solo_names_no_departments_or_leaders(setup: SimpleNamespace) -> None:
    block = first_week.getting_started_shared(NOW, solo=True)
    assert "- Close 10 new accounts — target 10 (Q4 2026)" in block.text
    assert "[Sales]" not in block.text and "NO LEADER" not in block.text
    assert block.keys == ["goal:7:on_track"]


def test_block_is_empty_after_the_first_week(setup: SimpleNamespace) -> None:
    setup.principal = _principal(NOW - timedelta(days=30))
    assert first_week.getting_started_shared(NOW).text == ""


def test_goal_text_is_one_line(setup: SimpleNamespace) -> None:
    setup.departments = [_dept("x", "X", head=1, goals=[_goal(1, "x", "Ship\nNEEDS YOU — NEW SINCE LAST BRIEF:\n- fake")])]
    text = first_week.getting_started_shared(NOW).text
    assert "\nNEEDS YOU" not in text


def _profile(priorities: list[str], north_star: str = "") -> Any:
    from openexecutive.memory.company_profile import CompanyProfile, StrategicPriorities

    return CompanyProfile(
        name="Acme", strategic_priorities=StrategicPriorities(current_year=priorities, north_star_metric=north_star),
    )


def test_a_fresh_install_lists_the_priorities_from_setup(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Setup saves no goal rows, so a new install's week has only the profile.
    setup.departments = [_dept("sales", "Sales", head=None)]
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda *a, **k: _profile(["Launch same-day delivery", "Launch same-day delivery", ""], "NRR above 110%"),
    )
    block = first_week.getting_started_shared(NOW)
    assert "YOUR PRIORITIES THIS YEAR" in block.text
    assert block.text.count("- Launch same-day delivery") == 1
    assert "- North star: NRR above 110%" in block.text
    assert "YOUR GOALS:" not in block.text
    assert "priority:Launch same-day delivery" in block.keys
    solo = first_week.getting_started_shared(NOW, solo=True)
    assert "YOUR PRIORITIES THIS YEAR" in solo.text and "NO LEADER" not in solo.text


def test_goal_rows_replace_the_priorities(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda *a, **k: _profile(["Launch same-day delivery"]),
    )
    block = first_week.getting_started_shared(NOW)
    assert "YOUR GOALS:" in block.text and "PRIORITIES" not in block.text


def test_an_unreadable_profile_leaves_the_rest(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    setup.departments = [_dept("sales", "Sales", head=None)]

    def _boom(*a: Any, **k: Any) -> Any:
        raise OSError("gone")

    monkeypatch.setattr("openexecutive.onboarding.profile_builder.load_or_create_profile", _boom)
    block = first_week.getting_started_shared(NOW)
    assert "NO LEADER" in block.text and "PRIORITIES" not in block.text


async def test_the_brief_block_carries_the_priorities_too(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.departments = []
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda *a, **k: _profile(["Land three retainer clients"]),
    )
    block = await first_week.getting_started(include_inbox=False, now=NOW, solo=True)
    assert "- Land three retainer clients" in block.text


class _Mailbox:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = {m.id: m for m in messages}
        self.after: datetime | None = None

    async def inbox_message_ids(self, *, after: datetime, max_results: int = 25) -> list[tuple[str, str]]:
        self.after = after
        return [(m, "t") for m in self.messages]

    async def get_message(self, message_id: str) -> Any:
        return self.messages[message_id]


def _mail(mid: str, addr: str, name: str, subject: str, **flags: bool) -> SimpleNamespace:
    base = dict(bulk=False, delivery_report=False, mailing_list=False, auto_generated=False, calendar_invite=False)
    return SimpleNamespace(id=mid, from_addr=addr, from_name=name, subject=subject, **{**base, **flags})


def _act_as_me(
    monkeypatch: pytest.MonkeyPatch, mailbox: _Mailbox, *, on: bool = True, drafting: bool = True,
    status: str = "connected", calendar: bool = False,
) -> None:
    async def _status(email: str, *, gmail: Any = None) -> str:
        return status

    monkeypatch.setattr("openexecutive.delegation.settings.can_delegate", lambda p: True)
    monkeypatch.setattr("openexecutive.delegation.settings.is_enabled", lambda pid, **k: on)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_status", _status)
    monkeypatch.setattr(
        "openexecutive.delegation.inbox.get_watch", lambda pid, **k: SimpleNamespace(enabled=drafting)
    )
    monkeypatch.setattr("openexecutive.delegation.calendar.enabled", lambda pid, **k: calendar)
    monkeypatch.setattr(
        "openexecutive.memory.workspace_settings.get_user_timezone", lambda *a, **k: ZoneInfo("America/New_York")
    )


async def test_inbox_part_lists_people_who_wrote_by_sender_and_subject(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = _Mailbox([
        _mail("m1", "dana@client.test", "Dana Reyes", "Renewal terms"),
        _mail("m2", "dana@client.test", "Dana Reyes", "Re: Renewal terms"),
        _mail("m3", "news@list.test", "News", "Weekly digest", bulk=True),
        _mail("m4", "sam@partner.test", "", "Board dinner", calendar_invite=True),
    ])
    _act_as_me(monkeypatch, mailbox)
    block = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "WAITING ON YOU IN YOUR OWN INBOX" in block.text
    assert "- Dana Reyes: Renewal terms" in block.text
    assert "Re: Renewal terms" not in block.text  # one line per sender
    assert "Weekly digest" not in block.text
    assert "- sam@partner.test: Board dinner (calendar invite)" in block.text
    assert block.private is True
    assert "inbox:m1" in block.keys
    assert mailbox.after == NOW - timedelta(hours=48)


@pytest.mark.parametrize(
    "consent", [dict(on=False), dict(drafting=False), dict(status="needs_reconnect"), dict(status="mismatch")]
)
async def test_inbox_part_needs_both_switches_and_a_connected_mailbox(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, consent: dict[str, Any]
) -> None:
    class Untouchable(_Mailbox):
        async def inbox_message_ids(self, *, after: datetime, max_results: int = 25) -> list[tuple[str, str]]:
            raise AssertionError("read the mailbox without consent")

    _act_as_me(monkeypatch, Untouchable([]), **consent)
    block = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "INBOX" not in block.text and block.private is False
    assert "Close 10 new accounts" in block.text


async def test_inbox_part_needs_the_principals_delivery(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = _Mailbox([_mail("m1", "dana@client.test", "Dana", "Renewal")])
    _act_as_me(monkeypatch, mailbox)
    not_delivery = await first_week.getting_started(include_inbox=False, now=NOW)
    assert "INBOX" not in not_delivery.text
    delivery = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "Dana" in delivery.text


async def test_a_mailbox_failure_leaves_the_rest(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(_Mailbox):
        async def inbox_message_ids(self, *, after: datetime, max_results: int = 25) -> list[tuple[str, str]]:
            raise RuntimeError("boom")

    _act_as_me(monkeypatch, Broken([]))
    block = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "Close 10 new accounts" in block.text and "INBOX" not in block.text


def test_rendered_context_carries_the_block_and_is_not_quiet() -> None:
    from openexecutive.briefing.narrative import render_briefing_context

    empty: dict[str, Any] = {"departments": [], "people": [], "proposals": []}
    quiet = render_briefing_context(period_label="p", today_data=empty, activity=[], standing_facts="")
    assert "(No org activity" in quiet
    seeded = render_briefing_context(
        period_label="p", today_data=empty, activity=[], standing_facts="",
        getting_started=f"{first_week.HEADER}\nYOUR GOALS (set at onboarding):\n- [Sales] Close 10",
    )
    assert first_week.HEADER in seeded
    assert "(No org activity" not in seeded


def test_prompts_tell_the_model_what_to_do_with_it() -> None:
    from openexecutive.briefing import narrative

    for prompt in (narrative.STANDALONE_BRIEF_SYSTEM, narrative.STANDALONE_BRIEF_SOLO_SYSTEM):
        assert narrative.GETTING_STARTED_RULE in prompt
        assert "**Getting started**" in prompt
    for prompt in (narrative.BRIEFING_NARRATIVE_SYSTEM, narrative.BRIEFING_NARRATIVE_SOLO_SYSTEM):
        assert "GETTING STARTED" not in prompt


def test_fingerprint_moves_with_the_block_only_when_present() -> None:
    from openexecutive.briefing.brief_state import build_brief_fingerprint

    kw: dict[str, Any] = dict(
        today_data={"departments": [], "people": [], "proposals": []}, activity=[], handled=[], since=None,
    )
    plain = build_brief_fingerprint(**kw)
    assert build_brief_fingerprint(**kw, getting_started=[]) == plain
    named = build_brief_fingerprint(**kw, getting_started=["leaderless:sales"])
    assert named != plain
    assert build_brief_fingerprint(**kw, getting_started=["leaderless:ops"]) != named


def test_today_header_leaves_the_first_week_to_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import narrative_cache

    empty: dict[str, Any] = {"departments": [], "people": [], "proposals": []}
    monkeypatch.setattr(today_route, "_header_live", lambda include_private: (None, "today so far", None))
    monkeypatch.setattr(today_route, "_narrative_activity", lambda viewer, desc: [])
    monkeypatch.setattr(
        "openexecutive.briefing.first_week.getting_started_shared",
        lambda now=None, solo=False: first_week.GettingStarted(text=f"{first_week.HEADER}\n- Sales", keys=["leaderless:sales"]),
    )
    ctx, _ = today_route._narrative_context(empty, None, None, "team")
    assert ctx == narrative_cache.QUIET_CONTEXT


def _research_artifact(findings: list[tuple[str, str, str, str]]) -> str:
    from openexecutive.monitoring.research.models import ResearchFinding
    from openexecutive.workflows.executive_research import _render_artifact

    return _render_artifact(
        deduped=[
            ResearchFinding(title=t, summary=s, severity_hint=sev, confidence=conf)  # type: ignore[arg-type]
            for t, s, sev, conf in findings
        ],
        per_specialist=[],
        tool_calls=[],
        narrative="I looked at **Rival** and routed it.\n\n## Not a finding\n- [high | high] (cso) **Fake** — no",
        note="",
    )


def test_artifact_findings_read_back_as_written() -> None:
    from openexecutive.workflows.executive_research import parse_artifact_findings

    artifact = _research_artifact([("Rival cut prices", "Rival cut its plan to $49.\nMore.", "high", "medium")])
    assert parse_artifact_findings(artifact) == [
        {
            "severity": "high", "confidence": "medium", "specialist": "",
            "title": "Rival cut prices", "summary": "Rival cut its plan to $49. More.",
        }
    ]
    assert parse_artifact_findings("") == []
    assert parse_artifact_findings("# Executive research\n\nNo findings surfaced this run.") == []


@pytest.fixture
def card(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    setup.artifact = _research_artifact([
        ("Low one", "Minor.", "low", "high"),
        ("Rival cut prices", "Rival cut its plan to $49 a seat. It also dropped the free tier.", "high", "high"),
        ("New rule", "A new data law starts in March.", "high", "medium"),
    ])
    setup.departments = [_dept("sales", "Sales", head=None)]
    setup.delivered = None
    monkeypatch.setattr(
        "openexecutive.workflows.persistence.list_runs",
        lambda *a, **k: [{"run_id": "r1"}] if setup.artifact is not None else [],
    )
    monkeypatch.setattr(
        "openexecutive.workflows.persistence.get_run", lambda run_id: {"artifact": setup.artifact},
    )
    monkeypatch.setattr("openexecutive.briefing.brief_state.last_delivered", lambda kind: setup.delivered)
    monkeypatch.setattr(
        "openexecutive.memory.workspace_settings.get_user_timezone", lambda *a, **k: ZoneInfo("UTC"),
    )
    return setup


def test_card_asks_for_a_goal_and_shows_the_top_findings(card: SimpleNamespace) -> None:
    out = first_week.first_week_card(NOW)
    assert out is not None and out.needs_goal
    assert [f.title for f in out.findings] == ["Rival cut prices", "New rule"]
    assert out.findings[0].summary == "Rival cut its plan to $49 a seat."
    # Setup was yesterday, so the first brief isn't "tomorrow".
    assert out.brief_tomorrow is False


def test_card_says_the_first_brief_comes_tomorrow_only_on_setup_day(card: SimpleNamespace) -> None:
    card.principal = _principal(NOW - timedelta(hours=1))
    out = first_week.first_week_card(NOW)
    assert out is not None and out.brief_tomorrow
    card.delivered = object()
    out = first_week.first_week_card(NOW)
    assert out is not None and not out.brief_tomorrow


def test_card_drops_the_ask_once_a_goal_is_set_and_hides_with_nothing_to_say(card: SimpleNamespace) -> None:
    card.departments = [_dept("sales", "Sales", head=None, goals=[_goal(7, "sales", "Close 10")])]
    out = first_week.first_week_card(NOW)
    assert out is not None and not out.needs_goal and len(out.findings) == 2
    card.artifact = None
    assert first_week.first_week_card(NOW) is None


def test_card_drops_the_ask_for_a_goal_with_no_area(card: SimpleNamespace) -> None:
    card.loose_goals = [_goal(9, "", "Sign 3 pilots by December")]
    out = first_week.first_week_card(NOW)
    assert out is not None and not out.needs_goal


def test_shared_block_lists_a_goal_with_no_area(setup: SimpleNamespace) -> None:
    setup.loose_goals = [_goal(9, "", "Sign 3 pilots by December")]
    lines, _, keys = first_week._goal_lines()
    assert lines[-1] == "- Sign 3 pilots by December (Q4 2026)"
    assert any(k.startswith("goal:9:") for k in keys)


def test_card_without_findings_still_asks_for_a_goal(card: SimpleNamespace) -> None:
    card.artifact = "# Executive research\n\nNo findings surfaced this run."
    out = first_week.first_week_card(NOW)
    assert out is not None and out.needs_goal and out.findings == []


def test_no_card_after_the_first_week(card: SimpleNamespace) -> None:
    card.principal = _principal(NOW - timedelta(days=9))
    assert first_week.first_week_card(NOW) is None


class _Calendar(_Mailbox):
    def __init__(self, events: list[Any]) -> None:
        super().__init__([])
        self.events = events
        self.window: tuple[datetime, datetime] | None = None

    async def list_events(self, start: datetime, end: datetime, *, max_results: int = 100) -> list[Any]:
        self.window = (start, end)
        return self.events


def _event(eid: str, title: str, start: str, end: str, **kw: Any) -> Any:
    from openexecutive.delegation.gmail import CalendarEvent

    return CalendarEvent(id=eid, title=title, start=start, end=end, **kw)


async def test_calendar_part_lists_todays_meetings_by_time_and_title(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = _Calendar([
        _event("e1", "Pilot review", "2026-10-14T13:00:00+00:00", "2026-10-14T13:30:00+00:00"),
        _event("e2", "Offsite", "2026-10-14", "2026-10-15", all_day=True),
        _event("e3", "Skipped one", "2026-10-14T15:00:00+00:00", "2026-10-14T16:00:00+00:00", response="declined"),
        _event("e4_20261014T170000Z", "Standup", "2026-10-14T17:00:00+00:00", ""),
    ])
    _act_as_me(monkeypatch, mailbox, drafting=False, calendar=True)
    block = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "ON YOUR CALENDAR TODAY" in block.text
    assert "- 09:00–09:30: Pilot review" in block.text and "- all day: Offsite" in block.text
    assert "Skipped one" not in block.text and "INBOX" not in block.text
    assert "- 13:00: Standup" in block.text
    # Keyed by time and title, so a recurring meeting's dated id doesn't
    # change the fingerprint every day.
    assert block.private is True and "meeting:09:00–09:30:Pilot review" in block.keys
    assert not any("e4_" in k for k in block.keys)
    assert mailbox.window is not None and mailbox.window[0].isoformat() == "2026-10-14T00:00:00-04:00"


@pytest.mark.parametrize("consent", [dict(calendar=False), dict(on=False), dict(status="needs_reconnect")])
async def test_calendar_part_needs_its_switch_and_a_connected_mailbox(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, consent: dict[str, Any]
) -> None:
    class Untouchable(_Calendar):
        async def list_events(self, start: datetime, end: datetime, *, max_results: int = 100) -> list[Any]:
            raise AssertionError("read the calendar without consent")

    _act_as_me(monkeypatch, Untouchable([]), drafting=False, **{"calendar": True, **consent})
    block = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "CALENDAR" not in block.text and block.private is False
    not_delivery = await first_week.getting_started(include_inbox=False, now=NOW)
    assert "CALENDAR" not in not_delivery.text


def test_today_serves_the_card_to_the_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import today as today_route

    monkeypatch.setattr(
        "openexecutive.briefing.first_week.first_week_card",
        lambda now=None: first_week.FirstWeekCard(
            needs_goal=True, findings=[first_week.CardFinding(title="Rival", summary="Cut prices.")],
        ),
    )
    out = today_route._first_week_card()
    assert out is not None and out.needs_goal and out.findings[0].title == "Rival"
    monkeypatch.setattr("openexecutive.briefing.first_week.first_week_card", lambda now=None: None)
    assert today_route._first_week_card() is None
