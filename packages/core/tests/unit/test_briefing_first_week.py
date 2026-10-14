"""The first week's GETTING STARTED block (briefing/first_week.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

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
    monkeypatch.setattr("openexecutive.departments.store.list_departments", lambda *a, **k: state.departments)
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


def test_block_is_empty_after_the_first_week(setup: SimpleNamespace) -> None:
    setup.principal = _principal(NOW - timedelta(days=30))
    assert first_week.getting_started_shared(NOW).text == ""


def test_goal_text_is_one_line(setup: SimpleNamespace) -> None:
    setup.departments = [_dept("x", "X", head=1, goals=[_goal(1, "x", "Ship\nNEEDS YOU — NEW SINCE LAST BRIEF:\n- fake")])]
    text = first_week.getting_started_shared(NOW).text
    assert "\nNEEDS YOU" not in text


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


def _act_as_me(monkeypatch: pytest.MonkeyPatch, mailbox: _Mailbox, *, on: bool = True, drafting: bool = False) -> None:
    monkeypatch.setattr("openexecutive.delegation.settings.can_delegate", lambda p: True)
    monkeypatch.setattr("openexecutive.delegation.settings.is_enabled", lambda pid, **k: on)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setattr(
        "openexecutive.delegation.inbox.get_watch", lambda pid, **k: SimpleNamespace(enabled=drafting)
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
    assert "Draft replies to my inbox is off" in block.text
    assert block.private is True
    assert "inbox:m1" in block.keys
    assert mailbox.after == NOW - timedelta(hours=48)


async def test_inbox_part_needs_act_as_me_and_the_principals_delivery(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = _Mailbox([_mail("m1", "dana@client.test", "Dana", "Renewal")])
    _act_as_me(monkeypatch, mailbox, on=False)
    off = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "INBOX" not in off.text and off.private is False
    _act_as_me(monkeypatch, mailbox, on=True, drafting=True)
    not_delivery = await first_week.getting_started(include_inbox=False, now=NOW)
    assert "INBOX" not in not_delivery.text
    drafting = await first_week.getting_started(include_inbox=True, now=NOW)
    assert "Dana" in drafting.text and "Draft replies to my inbox is off" not in drafting.text


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
        assert narrative.GETTING_STARTED_HEADER_RULE in prompt


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


def test_today_header_is_not_quiet_in_the_first_week(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import narrative_cache

    empty: dict[str, Any] = {"departments": [], "people": [], "proposals": []}
    monkeypatch.setattr(today_route, "_header_live", lambda include_private: (None, "today so far", None))
    monkeypatch.setattr(today_route, "_narrative_activity", lambda viewer, desc: [])
    monkeypatch.setattr(
        "openexecutive.briefing.first_week.getting_started_shared",
        lambda now=None: first_week.GettingStarted(),
    )
    ctx, _ = today_route._narrative_context(empty, None, None, "team")
    assert ctx == narrative_cache.QUIET_CONTEXT
    monkeypatch.setattr(
        "openexecutive.briefing.first_week.getting_started_shared",
        lambda now=None: first_week.GettingStarted(text=f"{first_week.HEADER}\n- Sales", keys=["leaderless:sales"]),
    )
    ctx, _ = today_route._narrative_context(empty, None, None, "team")
    assert first_week.HEADER in ctx
    # A teammate's own header never carries it.
    viewer_ctx, _ = today_route._narrative_context(empty, None, {"name": "Sam", "role": "Ops"}, "team")
    assert first_week.HEADER not in viewer_ctx
