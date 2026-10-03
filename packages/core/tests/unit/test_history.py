"""Always in the loop's store (memory/history.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.memory import episodic
from openexecutive.memory import history as h

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    return path


def _note(summary: str = "Told Dana Lee the price list comes Friday.") -> h.NewNote:
    return h.NewNote(kind="promised", summary=summary, quote="I'll send the price list on Friday")


def _add(person: int = 1, thread: str = "t1", when: datetime = NOW, summary: str | None = None) -> list[int]:
    return h.add_notes(
        person, [_note(summary) if summary else _note()], source=h.SOURCE_APPROVED_REPLY,
        channel=h.CHANNEL_EMAIL, conversation_ref=thread, counterpart="Dana Lee <dana@acme.example>",
        subject="Price list", trust="high", occurred_at=when, now=when,
    )


def test_settings_default_off_and_ninety_days() -> None:
    assert h.person_settings(1) == h.PersonSettings(reply_notes=False, retention_days=None)
    assert h.company_retention() == 90
    assert h.effective_retention(1) == 90


def test_notes_are_the_persons_own_and_expire() -> None:
    [nid] = _add()
    note = h.get_note(1, nid, now=NOW)
    assert note is not None and note.visibility == "private" and note.trust == "high"
    assert note.expires_at == (NOW + timedelta(days=90)).isoformat()
    assert h.get_note(2, nid, now=NOW) is None
    assert h.list_notes(2, now=NOW) == []
    later = NOW + timedelta(days=91)
    assert h.list_notes(1, now=later) == []
    assert h.sweep_expired(now=later) == 1
    assert h.list_notes(1, now=NOW) == []


def test_query_narrows_and_newest_first() -> None:
    _add(thread="a", when=NOW - timedelta(days=2), summary="Told Sam the venue is booked.")
    _add(thread="b", when=NOW - timedelta(days=1))
    assert [n.summary for n in h.list_notes(1, now=NOW)][0].startswith("Told Dana")
    assert [n.summary for n in h.list_notes(1, query="venue", now=NOW)] == ["Told Sam the venue is booked."]
    assert h.list_notes(1, query="dana acme", now=NOW)[0].counterpart.startswith("Dana")
    assert h.list_notes(1, query="%", now=NOW)


def test_pin_keeps_it_and_unpin_restores_expiry() -> None:
    [nid] = _add()
    pinned = h.pin_note(1, nid, True, now=NOW)
    assert pinned is not None and pinned.pinned and pinned.expires_at is None
    assert h.sweep_expired(now=NOW + timedelta(days=400)) == 0
    unpinned = h.pin_note(1, nid, False, now=NOW)
    assert unpinned is not None and unpinned.expires_at is not None
    assert h.pin_note(2, nid, True, now=NOW) is None


def test_unpinning_an_old_note_restarts_its_clock() -> None:
    [nid] = _add(when=NOW - timedelta(days=200))
    assert h.pin_note(1, nid, True, now=NOW - timedelta(days=199)) is not None
    unpinned = h.pin_note(1, nid, False, now=NOW)
    assert unpinned is not None and unpinned.expires_at == (NOW + timedelta(days=90)).isoformat()
    assert h.sweep_expired(now=NOW + timedelta(days=1)) == 0


def test_correct_and_forget_only_your_own() -> None:
    [nid] = _add()
    corrected = h.correct_note(1, nid, "  Told Dana it comes   Monday.\n", now=NOW)
    assert corrected is not None and corrected.correction == "Told Dana it comes Monday."
    cleared = h.correct_note(1, nid, "", now=NOW)
    assert cleared is not None and cleared.correction is None
    assert h.forget_note(2, nid) is False
    assert h.forget_note(1, nid) is True
    assert h.get_note(1, nid, now=NOW) is None


def test_forget_conversation_also_stops_new_notes() -> None:
    _add(thread="t1")
    _add(thread="t1")
    _add(thread="t2")
    key = h.conversation_key(h.CHANNEL_EMAIL, "t1")
    assert h.forget_conversation(1, key) == 2
    assert h.is_excluded(1, key) and not h.is_excluded(2, key)
    assert _add(thread="t1") == []
    assert len(h.list_notes(1, now=NOW)) == 1
    assert _add(person=2, thread="t1")


def test_person_retention_is_only_shorter() -> None:
    [nid] = _add()
    assert h.set_person_settings(1, by="t", retention_days=30).retention_days == 30
    note = h.get_note(1, nid, now=NOW)
    assert note is not None and note.expires_at == (NOW + timedelta(days=30)).isoformat()
    with pytest.raises(h.SettingError):
        h.set_person_settings(1, by="t", retention_days=365)
    with pytest.raises(h.SettingError):
        h.set_person_settings(1, by="t", retention_days=7)
    assert h.set_person_settings(1, by="t", reply_notes=True) == h.PersonSettings(True, 30)


def test_company_retention_clamps_people_and_until_forgotten() -> None:
    h.set_person_settings(1, by="t", retention_days=90)
    [nid] = _add()
    h.set_company_retention(30, by="owner")
    assert h.person_settings(1).retention_days == 30
    note = h.get_note(1, nid, now=NOW)
    assert note is not None and note.expires_at == (NOW + timedelta(days=30)).isoformat()
    h.set_company_retention(None, by="owner")
    h.set_person_settings(1, by="t", retention_days=None)
    assert h.effective_retention(1) is None
    note = h.get_note(1, nid, now=NOW)
    assert note is not None and note.expires_at is None
    with pytest.raises(h.SettingError):
        h.set_company_retention(True, by="owner")


def test_unknown_trust_or_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        h.add_notes(1, [_note()], source="x", channel="email", conversation_ref="t", counterpart="",
                    subject="", trust="certain", occurred_at=NOW)
    with pytest.raises(ValueError):
        h.add_notes(1, [h.NewNote("ordered", "x", "y")], source="x", channel="email", conversation_ref="t",
                    counterpart="", subject="", trust="high", occurred_at=NOW)


def test_tables_swap_with_a_client_slot() -> None:
    from openexecutive.clients import slots
    from openexecutive.memory.history_schema import TABLES

    assert set(TABLES) <= set(slots._BLANK_WIPE_TABLES)
