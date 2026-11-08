"""What the Executive remembers about one person (memory/person_recall.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.memory import episodic
from openexecutive.memory import history as h
from openexecutive.memory import person_recall as pr

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    rules = {1: ["Keep it short."], 2: ["Lead with the number."]}
    monkeypatch.setattr(
        "openexecutive.attunement.style.get_profile",
        lambda pid, **k: SimpleNamespace(rules=[SimpleNamespace(text=t) for t in rules.get(pid, [])]),
    )
    people = {
        1: SimpleNamespace(id=1, kind="team", archived=False, role="Founder", department_slugs=["growth"]),
        2: SimpleNamespace(id=2, kind="team", archived=False, role="Designer", department_slugs=[]),
        3: SimpleNamespace(id=3, kind="contact", archived=False, role="Buyer", department_slugs=[]),
    }
    monkeypatch.setattr("openexecutive.people.store.get_person", lambda pid: people.get(pid))
    return path


def _add(person: int, summary: str, *, counterpart: str = "", ago: int = 0) -> None:
    when = NOW - timedelta(days=ago)
    h.add_notes(
        person, [h.NewNote(kind="promised", summary=summary, quote=summary)],
        source=h.SOURCE_CHAT_MESSAGE, channel="web",
        conversation_ref="c1", counterpart=counterpart, subject="", trust="high", occurred_at=when, now=when,
    )


def test_own_memory_finds_matching_notes_and_style() -> None:
    _add(1, "Send the venue deposit to Sam on Friday.", ago=2)
    _add(1, "Agreed to move the customer review to Tuesday.", ago=1)
    text = pr.own_memory(1, "what did I say about the venue?")
    assert "venue deposit" in text and "customer review" not in text
    assert "Keep it short." in text
    # Nothing specific asked: the newest notes.
    assert "customer review" in pr.own_memory(1, "what do you know about me?")


def test_own_memory_is_only_their_own() -> None:
    _add(2, "The venue is booked.")
    assert "venue" not in pr.own_memory(1, "venue")


def test_own_memory_empty_when_nothing_known() -> None:
    assert pr.own_memory(3, "anything about the venue?") == ""


def test_work_style_never_includes_notes() -> None:
    _add(2, "The venue is booked.")
    text = pr.work_style(2)
    assert "Role: Designer" in text and "Lead with the number." in text
    assert "venue" not in text


def test_work_style_empty_for_a_contact_or_unknown() -> None:
    assert pr.work_style(3) == ""
    assert pr.work_style(99) == ""


def test_answers_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(20):
        _add(1, f"Long venue note {i} " + "x" * 400)
    assert len(pr.own_memory(1, "venue")) <= pr.MAX_CHARS


def test_brief_insight_uses_work_style_only_when_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import insights

    person = SimpleNamespace(id=2, kind="team", archived=False)
    monkeypatch.setattr(h, "can_share_work_style", lambda p: True)
    monkeypatch.setattr(h, "shares_work_style", lambda pid: False)
    assert insights._local_snippet(person) == ""  # type: ignore[arg-type]
    monkeypatch.setattr(h, "shares_work_style", lambda pid: True)
    assert "Lead with the number." in insights._local_snippet(person)  # type: ignore[arg-type]


@pytest.mark.parametrize(("shares", "team_workspace", "expected"), [
    (True, True, True), (False, True, False), (True, False, False),
])
def test_brief_insight_context_from_shared_work_style(
    monkeypatch: pytest.MonkeyPatch, shares: bool, team_workspace: bool, expected: bool,
) -> None:
    """With Honcho empty, only a shared work style in a team workspace
    reaches the insight prompt's relationship context."""
    import asyncio

    from openexecutive.people import insights

    sent: dict[str, str] = {}

    class _Provider:
        async def messages_create(self, **kw: object) -> object:
            sent["content"] = kw["messages"][0]["content"]  # type: ignore[index]
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="Prefers numbers first")])

    async def _no_honcho(_p: object) -> str:
        return ""

    monkeypatch.setattr(insights, "_honcho_snippet", _no_honcho)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: _Provider())
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "m")
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(h, "shares_work_style", lambda pid, **k: shares)
    monkeypatch.setattr(
        "openexecutive.memory.workspace_settings.get_workspace",
        lambda: SimpleNamespace(mode="team" if team_workspace else "solo"),
    )
    person = SimpleNamespace(id=2, kind="team", archived=False)
    asyncio.run(insights.generate_person_insight(person, {}))  # type: ignore[arg-type]
    assert ("Lead with the number." in sent["content"]) is expected
    assert ("RELATIONSHIP CONTEXT" in sent["content"]) is expected
