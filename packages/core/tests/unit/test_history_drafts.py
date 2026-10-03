"""Always in the loop in Act as me drafts (memory/history_drafts.py): only the
writer's own email notes that every recipient of the new draft was on."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.memory import episodic
from openexecutive.memory import history as h
from openexecutive.memory.history_drafts import notes_for_draft

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    h.set_person_settings(1, by="t", reply_notes=True)
    return path


def _note(quote: str, counterpart: str, *, person: int = 1, when: datetime = NOW,
          source: str = h.SOURCE_APPROVED_REPLY, due: str | None = None) -> int:
    [nid] = h.add_notes(
        person, [h.NewNote("promised", f"Said {quote}.", quote, due)], source=source,
        channel=h.CHANNEL_EMAIL if source == h.SOURCE_APPROVED_REPLY else "slack",
        conversation_ref=quote, counterpart=counterpart, subject="", trust="high", occurred_at=when,
    )
    return nid


def test_only_notes_every_recipient_was_on() -> None:
    _note("the pilot starts Oct 12", "Dana Lee <dana@acme.example>", due="2026-10-12")
    _note("the budget is 40k", "Dana Lee <dana@acme.example>, Bob <bob@acme.example>", when=NOW - timedelta(days=1))
    _note("Dana is hard work", "Carol <carol@other.example>")
    _note("I'll call Dana", "", source=h.SOURCE_CHAT_MESSAGE)
    _note("my own note", "Dana Lee <dana@acme.example>", person=2)

    to_dana = notes_for_draft(1, ["Dana@Acme.example"])
    assert to_dana is not None
    lines = to_dana.splitlines()
    assert lines[0].endswith('promised: "the pilot starts Oct 12" (due 2026-10-12)')
    assert "the budget is 40k" in lines[1]
    assert "hard work" not in to_dana and "call Dana" not in to_dana and "own note" not in to_dana

    # Bob was on the budget mail but not the start date: only the budget.
    both = notes_for_draft(1, ["dana@acme.example", "bob@acme.example"])
    assert both is not None and "40k" in both and "Oct 12" not in both
    # Someone who heard none of it gets nothing.
    assert notes_for_draft(1, ["dana@acme.example", "carol@other.example"]) is None


def test_a_correction_is_used_and_the_switch_off_reads_nothing() -> None:
    nid = _note("the pilot starts Oct 12", "dana@acme.example")
    h.correct_note(1, nid, "the pilot starts Oct 19")
    out = notes_for_draft(1, ["dana@acme.example"])
    assert out is not None and "Oct 19" in out and "Oct 12" not in out
    h.set_person_settings(1, by="t", reply_notes=False)
    assert notes_for_draft(1, ["dana@acme.example"]) is None
    assert notes_for_draft(None, ["dana@acme.example"]) is None
    assert notes_for_draft(1, []) is None


def test_at_most_eight_newest_first() -> None:
    for n in range(12):
        _note(f"item {n:02d} is fine", "dana@acme.example", when=NOW - timedelta(hours=n))
    out = notes_for_draft(1, ["dana@acme.example"])
    assert out is not None
    assert len(out.splitlines()) == 8 and "item 00" in out.splitlines()[0] and "item 11" not in out


def test_a_recipient_given_with_a_name_still_matches() -> None:
    _note("the pilot starts Oct 12", "Dana Lee <dana@acme.example>")
    out = notes_for_draft(1, ["Dana Lee <Dana@acme.example>"])
    assert out is not None and "Oct 12" in out
