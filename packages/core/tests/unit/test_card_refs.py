"""Cards named by their headline, never their number (alerts/card_refs.py)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.alerts import card_refs
from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import ARTIFACT_SOURCE, PRIVATE_ALERT_TAG
from openexecutive.briefing.grounding import extract_figures

BASE = "https://app.example.com"


@pytest.fixture()
def cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    db_path = tmp_path / "alerts.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    alert_store.initialize_db(db_path)
    monkeypatch.setattr(card_refs, "_base", lambda: BASE)

    def add(headline: str, **kw: object) -> int:
        alert_id = alert_store.insert_alert(
            source=str(kw.pop("source", "email")), external_id=headline, severity="medium",
            headline=headline, body="", **kw,  # type: ignore[arg-type]
        )
        assert alert_id is not None
        return alert_id

    return {
        "lease": add("Harbor Street lease renewal"),
        "budget": add("Q3 budget sign-off"),
        "private": add("Note from a contact", topic_tags=[PRIVATE_ALERT_TAG]),
        "doc": add("Board memo draft", source=ARTIFACT_SOURCE, owner_person_id=7),
    }


def _for(text: str, *, person: int | None = 1, principal: bool = True, **kw: object) -> str:
    return card_refs.name_cards(text, viewer_person_id=person, is_principal=principal, **kw)  # type: ignore[arg-type]


def test_a_card_number_becomes_its_headline_linked_to_home(cards: dict[str, int]) -> None:
    n = cards["lease"]
    assert _for(f"Review card {n} today.") == (
        f"Review the [“Harbor Street lease renewal”]({BASE}/?card={n}) card today."
    )
    assert _for(f"Card #{n} waits.").startswith("The [“Harbor Street lease renewal”]")
    assert _for(f"Approve the card {n}.") == (
        f"Approve the [“Harbor Street lease renewal”]({BASE}/?card={n}) card."
    )
    assert _for(f"alert_id={n}", fmt="plain") == "the “Harbor Street lease renewal” card"


def test_several_cards_and_slack_links(cards: dict[str, int]) -> None:
    a, b = cards["lease"], cards["budget"]
    assert _for(f"Cards {a} and {b} wait.", fmt="slack") == (
        f"The <{BASE}/?card={a}|“Harbor Street lease renewal”> and "
        f"<{BASE}/?card={b}|“Q3 budget sign-off”> cards wait."
    )


def test_numbers_that_are_no_card_stay_as_written(cards: dict[str, int]) -> None:
    a = cards["lease"]
    assert _for(f"cards {a} and 4242") == f"cards {a} and 4242"
    one = cards["lease"]
    for text in (f"Set an alert {one} minutes before.", f"card {one} of 5", f"alert {one}% done"):
        assert _for(text) == text
    for text in ("Paid with card 4242.", "See /cards/1 for it.", "Your scorecard 5 is fine.", "no cards here"):
        assert _for(text) == text


def test_a_card_the_reader_may_not_see_is_never_named(cards: dict[str, int]) -> None:
    # Private to the principal: named only when the message is theirs alone.
    p = cards["private"]
    assert _for(f"Card {p} waits.") == "That card waits."
    assert "Note from a contact" in _for(f"Card {p} waits.", include_private=True)
    assert _for(f"Card {p} waits.", person=2, principal=False, include_private=True) == "That card waits."
    # A teammate's drafted document: only its owner sees it.
    d = cards["doc"]
    assert _for(f"card {d}") == "that card"
    assert "Board memo draft" in _for(f"card {d}", person=7, principal=False)
    # Someone off the roster sees no headline at all.
    assert _for(f"card {cards['lease']}", person=None, principal=False) == "that card"


def test_name_cards_for_works_out_the_principal(cards: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import store as people_store

    monkeypatch.setattr(people_store, "find_principal_person", lambda: SimpleNamespace(id=1))
    p = cards["private"]
    assert "Note from a contact" in card_refs.name_cards_for(f"card {p}", None, principal=True, include_private=True)
    assert card_refs.name_cards_for(f"card {p}", None, include_private=True) == "that card"
    assert card_refs.name_cards_for(f"card {p}", 2, include_private=True) == "that card"


def test_no_link_without_a_web_address(cards: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(card_refs, "_base", lambda: "")
    assert _for(f"card {cards['lease']}") == "the “Harbor Street lease renewal” card"
    assert card_refs.on_home_note() == "on Home"


def test_link_format_by_channel() -> None:
    assert card_refs.link_format("slack") == "slack"
    assert card_refs.link_format("slack_dm") == "slack"
    assert card_refs.link_format("google_chat") == "slack"
    for channel in ("telegram", "discord", "email", "", None):
        assert card_refs.link_format(channel) == "markdown"


def test_slack_links() -> None:
    assert card_refs.slack_links("Open [Home](https://a.example/) now") == "Open <https://a.example/|Home> now"
    assert card_refs.slack_links("[x](javascript:alert(1))") == "[x](javascript:alert(1))"


def test_grounding_never_checks_or_cites_a_card_number() -> None:
    assert extract_figures("Review card 220 and alert #31, then alert_id=44.") == []
    assert [f.raw for f in extract_figures("Raised 220 leads.")] == ["220"]


def test_a_brief_names_cards_for_whoever_reads_it() -> None:
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.workflows.morning_brief import PRINCIPAL_DELIVERY, _card_reader

    token = PRINCIPAL_DELIVERY.set(True)
    try:
        assert _card_reader() == (None, True)
    finally:
        PRINCIPAL_DELIVERY.reset(token)
    assert _card_reader() == (None, False)
    session = current_session.set(SimpleNamespace(caller_person_id=5))  # type: ignore[arg-type]
    try:
        assert _card_reader() == (5, False)
    finally:
        current_session.reset(session)


async def test_a_slack_dm_delivery_gets_slack_links(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from openexecutive.orchestrator import schedule_tools
    from openexecutive.scheduler import runner

    sent: list[str] = []

    async def _slack(args: dict[str, object]) -> str:
        sent.append(str(args["text"]))
        return json.dumps({"status": "failed", "error": "test"})

    monkeypatch.setattr(schedule_tools, "handle_send_slack_dm", _slack)
    person = SimpleNamespace(id=1, slack_user_id="U1")
    await runner._send_on_plan(person, ["slack_dm"], "Open [Home](https://app.example.com/)", label="Brief")  # type: ignore[arg-type]
    assert sent == ["Open <https://app.example.com/|Home>"]


def test_a_hidden_card_in_a_list_still_counts(cards: dict[str, int]) -> None:
    a, p = cards["lease"], cards["private"]
    assert _for(f"cards {a} and {p}", fmt="plain") == "the “Harbor Street lease renewal” card and 1 other"


def test_the_default_local_address_is_no_link(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive import config

    for url, expected in (
        ("http://localhost:3000", ""),
        ("http://127.0.0.1:8000/", ""),
        ("https://app.example.com/", "https://app.example.com/?card=3"),
    ):
        monkeypatch.setattr(config, "get_settings", lambda u=url: SimpleNamespace(ui_base_url=u))
        assert card_refs.card_url(3) == expected


def test_a_chat_reply_names_private_cards_only_to_the_principal_alone(
    cards: dict[str, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.people import store as people_store

    monkeypatch.setattr(people_store, "find_principal_person", lambda: SimpleNamespace(id=1))
    p = cards["private"]
    dm = SimpleNamespace(origin_channel="slack", private_chat=True, speaker_verified=True)
    group = SimpleNamespace(origin_channel="slack", private_chat=False)
    named = card_refs.name_cards_in_reply(f"card {p}", dm, 1, None)
    assert named == f"the <{BASE}/?card={p}|“Note from a contact”> card"
    assert card_refs.name_cards_in_reply(f"card {p}", dm, 1, [1, 2]) == "that card"
    assert card_refs.name_cards_in_reply(f"card {p}", group, 1, None) == "that card"
    assert card_refs.name_cards_in_reply(f"card {p}", dm, 2, None) == "that card"


def test_a_headline_brings_no_link_or_mention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "alerts.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    alert_store.initialize_db(db_path)
    n = alert_store.insert_alert(
        source="email", external_id="x", severity="medium",
        headline="Pay now at https://pay.example/x via www.pay.example ask @channel", body="",
    )
    assert _for(f"card {n}", fmt="plain") == "the “Pay now at via ask channel” card"


def test_a_link_to_a_card_is_not_renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "John asked about the budget. [Answer](https://app.example.com/?card=4)"
    assert card_refs._MENTION.search(text) is None



def test_write_up_url_links_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(card_refs, "_base", lambda: "https://app.example.com")
    assert card_refs.write_up_url("run:r1") == "https://app.example.com/artifacts/run%3Ar1"
    assert card_refs.write_up_url("") == ""
