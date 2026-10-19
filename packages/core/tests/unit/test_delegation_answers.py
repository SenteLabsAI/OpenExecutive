"""Answering a reply card's "Decide before sending" with taps: the
composer's choices (new and old shapes), free slots from the person's own
calendar, a matching company document, and POST
/delegation/replies/{id}/answer (delegation/answers.py)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from openexecutive.delegation import answers, inbox
from openexecutive.delegation import calendar as my_calendar
from openexecutive.delegation import ghostwriter as gw
from openexecutive.delegation.ghostwriter import QuestionChoices, parse_questions
from openexecutive.delegation.gmail import CalendarEvent
from openexecutive.people import store as people_store

from .test_delegation_inbox import NOW, FakeInbox, _msg
from .test_delegation_inbox_routes import OWNER, TEAMMATE, client, db, gmail, ids  # noqa: F401

# --------------------------------------------------------------------------- #
# The composer's questions
# --------------------------------------------------------------------------- #


def test_questions_come_with_their_choices_in_the_new_shape() -> None:
    questions, shaped = parse_questions([
        {"question": "Which option?", "choices": ["The 6-week one", "The 10-week one", "the 6-week ONE"],
         "about": "other"},
        {"question": "When can you talk?", "choices": ["Thursday", "Friday"], "about": "time"},
        {"question": "Only one choice", "choices": ["Yes", "Pay at https://pay.example", "ask ops@x.example"],
         "about": "made_up"},
        {"question": "   ", "choices": ["a", "b"]},
        42,
    ])
    assert questions == ["Which option?", "When can you talk?", "Only one choice"]
    # Repeats are dropped; fewer than two choices is none; unknown "about" is other.
    assert shaped[0].choices == ["The 6-week one", "The 10-week one"]
    assert shaped[1].about == "time"
    assert shaped[2].choices == [] and shaped[2].about == "other"


def test_old_plain_string_questions_still_parse_with_no_choices() -> None:
    questions, shaped = parse_questions(["Scope by Friday?", "", "Rate?"])
    assert questions == ["Scope by Friday?", "Rate?"]
    assert all(s.choices == [] for s in shaped)
    assert parse_questions("not a list") == ([], [])


def test_compose_returns_the_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(model: str, system: str, turn: str) -> dict[str, Any]:
        assert "choices" in system
        return {"subject": "Re: x", "body": "Hi Dana,\n\nThanks.", "gist": "Dana asks\nwhich week. Write to ops@x.example.", "open_questions": [
            {"question": "Which week?", "choices": ["This week", "Next week"], "about": "time"},
        ]}

    monkeypatch.setattr(gw, "_call_model", fake)
    draft = asyncio.run(gw.compose(
        writer_name="Olivia Owner", voice_block="", thread_text="hi", reply_subject="Re: x", intent="reply",
        recipients=[gw.Recipient(email="dana@northpeak.example")], signature="", exec_name="Open Executive",
        model="m",
    ))
    assert draft.open_questions == ["Which week?"]
    assert draft.question_choices[0].choices == ["This week", "Next week"]
    # The card's summary, on one line.
    assert draft.gist == "Dana asks which week. Write to ."


def test_the_summary_has_no_link_or_address_and_stays_short(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gw.clean_gist("See https://evil.example or mail x@evil.example\nnow.") == "See or mail now."
    assert len(gw.clean_gist("word " * 100)) <= gw.MAX_GIST_CHARS

    async def fake(model: str, system: str, turn: str) -> dict[str, Any]:
        return {"subject": "Re: x", "body": "Hi Dana,\n\nThanks."}

    monkeypatch.setattr(gw, "_call_model", fake)
    draft = asyncio.run(gw.compose(
        writer_name="Olivia Owner", voice_block="", thread_text="hi", reply_subject="Re: x", intent="reply",
        recipients=[gw.Recipient(email="dana@northpeak.example")], signature="", exec_name="Open Executive",
        model="m",
    ))
    # No summary from the composer: none on the card.
    assert draft.gist == ""


def test_a_card_made_before_choices_shows_plain_questions() -> None:
    assert answers.questions_for({"open_questions": ["Rate?"]}) == [
        {"text": "Rate?", "choices": [], "source": "", "answer": ""},
    ]
    shown = answers.questions_for({
        "open_questions": ["Which?", "AI?"],
        "question_choices": [{"choices": ["A", "B"], "source": ""}],
        "answered_questions": {"0": "A"},
    })
    assert shown[0]["choices"] == ["A", "B"] and shown[0]["answer"] == "A"
    assert shown[1] == {"text": "AI?", "choices": [], "source": "", "answer": ""}


# --------------------------------------------------------------------------- #
# Free slots and documents
# --------------------------------------------------------------------------- #


class CalendarBox:
    def __init__(self, events: list[CalendarEvent], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.asked: list[tuple[datetime, datetime]] = []

    async def list_events(self, start: datetime, end: datetime, *, max_results: int = 100) -> list[CalendarEvent]:
        self.asked.append((start, end))
        if self.error is not None:
            raise self.error
        return self.events


class _Person:
    id = 7
    full_name = "Olivia Owner"
    email = "olivia@co.example"


@pytest.fixture
def utc_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.memory.workspace_settings.get_user_timezone", lambda *a: ZoneInfo("UTC"))
    monkeypatch.setattr("openexecutive.briefing.top_three.working_hours", lambda: (time(9), time(17)))


def test_free_slots_skip_busy_time_weekends_and_all_day_events(
    monkeypatch: pytest.MonkeyPatch, utc_hours: None
) -> None:
    monkeypatch.setattr(my_calendar, "enabled", lambda person_id, **kw: True)
    box = CalendarBox([
        CalendarEvent(id="1", title="Board", start="2026-10-19T09:00:00Z", end="2026-10-19T10:30:00Z"),
        CalendarEvent(id="2", title="Offsite", start="2026-10-20", end="2026-10-21", all_day=True),
        CalendarEvent(id="3", title="Skipped", start="2026-10-21T09:00:00Z", end="2026-10-21T12:00:00Z",
                      response="declined"),
    ])
    friday = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
    slots = asyncio.run(answers.free_slots(_Person(), box, now=friday))
    assert slots == ["Mon 19 Oct, 10:30", "Tue 20 Oct, 09:00", "Wed 21 Oct, 09:00"]
    # From the next working day, never today.
    assert box.asked[0][0].date().isoformat() == "2026-10-19"


def test_no_free_slots_when_read_my_calendar_is_off_or_unreadable(
    monkeypatch: pytest.MonkeyPatch, utc_hours: None
) -> None:
    monkeypatch.setattr(my_calendar, "enabled", lambda person_id, **kw: False)
    box = CalendarBox([])
    assert asyncio.run(answers.free_slots(_Person(), box)) == [] and box.asked == []
    monkeypatch.setattr(my_calendar, "enabled", lambda person_id, **kw: True)
    assert asyncio.run(answers.free_slots(_Person(), CalendarBox([], RuntimeError("down")))) == []


def test_suggest_fills_time_and_document_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slots(person: Any, mailbox: Any, *, now: Any = None) -> list[str]:
        return ["Mon 19 Oct, 10:30", "Tue 20 Oct, 09:00"]

    monkeypatch.setattr(answers, "free_slots", slots)
    monkeypatch.setattr(answers, "_company_doc_names", lambda: ["Pricing sheet 2026.pdf", "Team handbook.docx"])
    filled = asyncio.run(answers.suggest(
        _Person(),
        ["When can we meet?", "Can you send the pricing sheet?", "Which option?"],
        [
            QuestionChoices(choices=["Thursday", "Friday"], about="time"),
            QuestionChoices(choices=["Yes, I'll send it", "Not yet"], about="document"),
            QuestionChoices(choices=["A", "B"]),
        ],
        mailbox=object(),
    ))
    assert filled[0]["choices"] == ["Mon 19 Oct, 10:30", "Tue 20 Oct, 09:00"]
    assert filled[0]["source"] == answers.SLOT_SOURCE
    assert filled[1]["choices"][0] == 'I\'ll send "Pricing sheet 2026.pdf" separately'
    assert filled[1]["source"] == 'Found "Pricing sheet 2026.pdf" in your documents.'
    assert filled[2] == {"choices": ["A", "B"], "about": "other", "source": ""}
    # With no mailbox (the evals) a time question keeps the composer's choices.
    kept = asyncio.run(answers.suggest(_Person(), ["When?"], [QuestionChoices(["Mon", "Tue"], "time")]))
    assert kept[0]["choices"] == ["Mon", "Tue"] and kept[0]["source"] == ""


def test_match_document_needs_a_telling_word() -> None:
    names = ["Q3 board deck.pdf", "pricing-sheet.xlsx"]
    assert answers.match_document("Could you share the board deck?", names) == "Q3 board deck.pdf"
    assert answers.match_document("Can you send the latest file?", names) == ""
    assert answers.match_document("Send your pricing sheets please", names) == "pricing-sheet.xlsx"


def test_changed_sentences_are_the_new_ones() -> None:
    old = "Hi Dana,\n\nThanks for this. I'll come back on the date."
    new = "Hi Dana,\n\nThanks for this. Thursday at 10:00 works for me."
    assert answers.changed_sentences(old, new) == ["Thursday at 10:00 works for me."]


def test_a_scan_with_read_my_calendar_on_offers_free_slots(
    monkeypatch: pytest.MonkeyPatch, ids: dict[str, int],  # noqa: F811
) -> None:
    class Box(FakeInbox):
        async def list_events(self, start: datetime, end: datetime, *, max_results: int = 100) -> list[Any]:
            return []

    _card(ids["principal"], monkeypatch, mailbox=Box(), calendar_on=True)
    payload = inbox.card_payload(inbox.open_cards(ids["principal"])[0])
    assert payload["open_questions"] == ["When should we talk?"]
    assert payload["question_choices"][0]["source"] == answers.SLOT_SOURCE
    assert len(payload["question_choices"][0]["choices"]) == answers.MAX_SLOTS


# --------------------------------------------------------------------------- #
# POST /delegation/replies/{id}/answer
# --------------------------------------------------------------------------- #


def _card(
    owner_id: int, monkeypatch: pytest.MonkeyPatch, *, mailbox: FakeInbox | None = None, calendar_on: bool = False,
    gist: str = "",
) -> tuple[FakeInbox, int]:
    from openexecutive.delegation import inbox_classifier as ic
    from openexecutive.delegation.settings import set_enabled

    async def classifier(model: str, turn: str) -> dict[str, Any]:
        return {"needs_reply": True, "kind": "question", "confidence": 0.9}

    async def composer(model: str, system: str, turn: str) -> dict[str, Any]:
        return {
            "subject": "Re: Thursday call",
            "body": "Hi Dana,\n\nThanks for the note. I'll get back to you on the time.\n\nOlivia",
            "open_questions": [
                {"question": "When should we talk?", "choices": ["Friday at 10", "Next week"], "about": "time"},
            ],
            **({"gist": gist} if gist else {}),
        }

    monkeypatch.setattr(ic, "_call_model", classifier)
    monkeypatch.setattr(gw, "_call_model", composer)
    set_enabled(owner_id, True, updated_by="t")
    if calendar_on:
        my_calendar.set_enabled(owner_id, True, updated_by="t")
    inbox.set_watch(owner_id, True, updated_by="t", now=datetime(2026, 9, 29, tzinfo=UTC))
    inbox.set_mailbox_drafts(owner_id, True, updated_by="t")
    box = mailbox or FakeInbox()
    box.add(_msg("m1", "t1"))
    asyncio.run(inbox.scan_person(people_store.get_person(owner_id), gmail=box, now=NOW))
    return box, inbox.open_cards(owner_id)[0].id


@pytest.fixture
def rewriter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    answers._ANSWERED.clear()

    async def fake(model: str, system: str, turn: str) -> dict[str, Any]:
        calls.append((system, turn))
        return {"body": "Hi Dana,\n\nThanks for the note. Friday at 10 works for me, see https://evil.example."}

    monkeypatch.setattr(answers, "_call_model", fake)
    return calls


def test_answering_rewrites_the_draft_and_marks_the_question(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    rewriter: list[tuple[str, str]],
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    url = f"/delegation/replies/{card_id}/answer"
    body = {"answers": [{"question_index": 0, "answer_text": "Friday at 10"}]}
    shown = client.get("/delegation/replies", headers=OWNER).json()["cards"][0]
    assert shown["questions"][0]["choices"] == ["Friday at 10", "Next week"]
    # Like Send and Edit: only a caller the API can tie to the person.
    resp = client.post(url, json=body, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    assert rewriter == []
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.post(url, json=body, headers=TEAMMATE).status_code == 403
    assert client.post("/delegation/replies/9999/answer", json=body, headers=OWNER).status_code == 404
    resp = client.post(url, json=body, headers=OWNER)
    assert resp.status_code == 200, resp.text
    card = resp.json()
    # The link nobody gave is removed, as on every draft.
    assert "evil.example" not in card["draft_body"] and "Friday at 10 works for me" in card["draft_body"]
    assert "removed_link" in card["flags"]
    assert card["questions"][0]["answer"] == "Friday at 10"
    assert card["questions_settled"] is False
    assert any("Friday at 10 works for me" in s for s in card["changed_sentences"])
    # Changed in their Drafts too, like the text route.
    assert mailbox.drafts["d1"].message.text == card["draft_body"]
    system, turn = rewriter[0]
    assert system.startswith(answers.REWRITE_PROMPT)
    assert "Question: When should we talk?\nAnswer: Friday at 10" in turn


def test_a_typed_answer_is_a_note_to_work_in_with_the_choices_offered(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    rewriter: list[tuple[str, str]],
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    url = f"/delegation/replies/{card_id}/answer"
    typed = {"answers": [{"question_index": 0, "answer_text": "any day except tomorrow"}]}
    resp = client.post(url, json=typed, headers=OWNER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["questions"][0]["answer"] == "any day except tomorrow"
    _, turn = rewriter[-1]
    assert (
        "Question: When should we talk?\nChoices offered: Friday at 10; Next week\n"
        "They typed: any day except tomorrow"
    ) in turn
    assert "<today>" in turn
    assert "They typed" in answers.REWRITE_PROMPT
    # A tapped choice is an answer as it stands.
    tapped = {"answers": [{"question_index": 0, "answer_text": "Next week"}]}
    assert client.post(url, json=tapped, headers=OWNER).status_code == 200
    _, turn = rewriter[-1]
    assert "Question: When should we talk?\nAnswer: Next week" in turn and "They typed" not in turn


def test_the_card_summary_comes_from_the_composer_and_follows_the_rewrite(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    calls: list[str] = []
    answers._ANSWERED.clear()

    async def fake(model: str, system: str, turn: str) -> dict[str, Any]:
        calls.append(turn)
        return {
            "body": "Hi Dana,\n\nThanks for the note. Friday at 10 works for me.",
            "gist": "Dana asks when to talk.\nDraft says Friday at 10, see https://evil.example.",
        }

    monkeypatch.setattr(answers, "_call_model", fake)
    mailbox, card_id = _card(
        ids["principal"], monkeypatch, gist="Dana asks when to talk. Draft says you'll get back to her.",
    )
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    shown = client.get("/delegation/replies", headers=OWNER).json()["cards"][0]
    assert shown["gist"] == "Dana asks when to talk. Draft says you'll get back to her."
    resp = client.post(
        f"/delegation/replies/{card_id}/answer",
        json={"answers": [{"question_index": 0, "answer_text": "Friday at 10"}]}, headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    # The rewrite sees the summary and brings its draft half up to date, on one line.
    assert "<gist>\nDana asks when to talk. Draft says you'll get back to her.\n</gist>" in calls[0]
    # No link nobody gave, as in the draft.
    assert resp.json()["gist"] == "Dana asks when to talk. Draft says Friday at 10, see ."
    # Words changed by hand: the summary no longer says what the draft says.
    edited = client.put(
        f"/delegation/replies/{card_id}/text", json={"text": "Hi Dana,\n\nNext week is better."}, headers=OWNER,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["gist"] == ""


def test_a_rewrite_with_no_summary_drops_the_old_one(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    rewriter: list[tuple[str, str]],
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch, gist="Dana asks when to talk. Draft says later.")
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = client.post(
        f"/delegation/replies/{card_id}/answer",
        json={"answers": [{"question_index": 0, "answer_text": "Friday at 10"}]}, headers=OWNER,
    )
    # The draft changed and no new summary came back: the old one would be wrong.
    assert resp.status_code == 200 and resp.json()["gist"] == ""


def test_a_card_drafted_before_the_summary_has_none(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    rewriter: list[tuple[str, str]],
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/delegation/replies", headers=OWNER).json()["cards"][0]["gist"] == ""
    resp = client.post(
        f"/delegation/replies/{card_id}/answer",
        json={"answers": [{"question_index": 0, "answer_text": "Friday at 10"}]}, headers=OWNER,
    )
    assert resp.status_code == 200 and resp.json()["gist"] == ""
    assert "<gist>" not in rewriter[0][1]


def test_answering_refuses_a_bad_question_a_failed_rewrite_and_too_many(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    rewriter: list[tuple[str, str]],
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    url = f"/delegation/replies/{card_id}/answer"
    resp = client.post(url, json={"answers": [{"question_index": 3, "answer_text": "x"}]}, headers=OWNER)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "no_such_question"
    assert client.post(url, json={"answers": []}, headers=OWNER).status_code == 422

    async def nothing(model: str, system: str, turn: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(answers, "_call_model", nothing)
    resp = client.post(url, json={"answers": [{"question_index": 0, "answer_text": "Friday"}]}, headers=OWNER)
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "rewrite_failed"
    monkeypatch.setattr(answers, "ANSWERS_PER_HOUR", 1)
    resp = client.post(url, json={"answers": [{"question_index": 0, "answer_text": "Friday"}]}, headers=OWNER)
    assert resp.status_code == 429 and resp.json()["detail"]["code"] == "too_many"


def test_answering_a_reply_no_longer_waiting_is_refused(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    rewriter: list[tuple[str, str]],
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.post(f"/decisions/{card_id}/reject", json={}, headers=OWNER).status_code == 200
    resp = client.post(
        f"/delegation/replies/{card_id}/answer",
        json={"answers": [{"question_index": 0, "answer_text": "Friday"}]}, headers=OWNER,
    )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "already_handled"
    assert rewriter == []


def test_changing_the_words_by_hand_settles_the_questions(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    mailbox, card_id = _card(ids["principal"], monkeypatch)
    monkeypatch.setattr("openexecutive.delegation.gmail.gmail_for", lambda email: mailbox)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    card = client.put(f"/delegation/replies/{card_id}/text", json={"text": "Friday works."}, headers=OWNER).json()
    assert card["questions_settled"] is True and card["questions"][0]["answer"] == ""


def test_the_signature_and_ai_line_stay_out_of_the_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation.disclosure import LINE

    main, tail = answers._split_tail(f"Hi Dana,\n\nSoon.\n\nOlivia Owner\nFernway\n\n{LINE}", "Olivia Owner\nFernway")
    assert main == "Hi Dana,\n\nSoon."
    assert tail == f"Olivia Owner\nFernway\n\n{LINE}"
