"""briefing.reader: every brief knows who reads it and keeps them "you".

The /today header was told nothing about its reader, and its inputs named
them like anyone else (their own chat, the Executive's replies to their
address, a chat titled with their name), so it wrote "John Johnson has been
messaging you nonstop" to John Johnson.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.briefing.live_signals import LiveSignals
from openexecutive.briefing.narrative import render_briefing_context
from openexecutive.briefing.reader import Reader, fix_slips, mask, slips

PAT = Reader("Pat Owner", ("pat@x.com", "Pat.Owner@alias.com"))


def test_mask_hides_the_readers_name_and_addresses_everywhere() -> None:
    text = (
        "Inbound google_chat from Pat Owner: any word?\n"
        "Replied by email to pat@x.com; also pat.owner@alias.com\n"
        "Chat: Google Chat Pat Owner\n"
        "Pat Owner's access, and Pat Ownership stays"
    )

    out = mask(text, PAT)

    assert "Pat Owner" not in out.replace("Pat Ownership", "")
    assert "pat@x.com" not in out and "alias.com" not in out
    assert "Inbound google_chat from the reader: any word?" in out
    assert "Google Chat the reader" in out
    assert "the reader's access" in out
    assert "Pat Ownership" in out  # whole words only


def test_a_bare_first_name_is_left_alone() -> None:
    """A first name names a teammate just as often; only the full name masks."""
    assert mask("Pat from finance wrote", PAT) == "Pat from finance wrote"
    assert Reader("Cher").terms() == []


def test_the_brief_that_named_its_reader_reads_as_you() -> None:
    draft = (
        "**Bottom line: Pat Owner has been messaging you nonstop since noon.**\n"
        "- **Pat Owner's access** — the principal is waiting on the reader's offer.\n"
        "**Move today:** Reply to pat@x.com with the figure."
    )

    assert slips(draft, PAT) == ["pat owner", "pat@x.com", "the principal", "the reader"]
    assert slips("the Executive replied", PAT) == ["the executive"]
    fixed = fix_slips(draft, PAT)

    assert slips(fixed, PAT) == []
    assert "**Bottom line: You have been messaging you" in fixed
    assert "- **Your access** — you are waiting on your offer." in fixed
    assert "Reply to you with the figure." in fixed


def test_no_reader_changes_nothing() -> None:
    assert mask("Pat Owner", None) == "Pat Owner"
    assert slips("the principal", None) == []
    assert fix_slips("the principal", None) == "the principal"


def test_the_context_names_its_reader_once_and_only_on_the_reader_line() -> None:
    live = LiveSignals(
        inbound=("[12:14] google_chat from Pat Owner: the St. Albans offer (3 messages)",),
        inbound_total=3, inbound_shown=3,
        conversations=("[12:18] Google Chat Pat Owner",),
    )
    activity = [{"at": "2026-10-16T15:43", "kind": "email", "summary": "Replied by email to pat@x.com"}]

    out = render_briefing_context(
        period_label="p", today_data={}, activity=activity, live=live,
        now_label="now", reader=PAT, standing_facts="",
    )

    lines = out.splitlines()
    assert lines[0] == "PERIOD: p" and lines[2] == "NOW: now"
    reader_lines = [ln for ln in lines if "Pat Owner" in ln or "pat@x.com" in ln]
    assert len(reader_lines) == 1 and reader_lines[0].startswith("THE READER")
    assert "google_chat from the reader: the St. Albans offer" in out
    assert "Google Chat the reader" in out
    assert "Replied by email to the reader" in out


class _Provider:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kw: Any) -> object:
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.texts.pop(0))])


async def _synth(monkeypatch: pytest.MonkeyPatch, texts: list[str]) -> tuple[str, _Provider]:
    from openexecutive import providers
    from openexecutive.briefing.narrative import synthesize_briefing_narrative

    provider = _Provider(texts)
    monkeypatch.setattr(providers, "get_provider", lambda _m: provider)
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    text = await synthesize_briefing_narrative(
        today_data={}, activity=[], period_label="p", rendered_context="ctx", reader=PAT,
    )
    return text, provider


@pytest.mark.asyncio
async def test_a_draft_that_names_its_reader_is_rewritten_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, provider = await _synth(monkeypatch, [
        "**Pat Owner is waiting on the offer.**",
        "**You're waiting on the offer.**",
    ])

    assert text == "**You're waiting on the offer.**"
    assert len(provider.calls) == 2
    retry = provider.calls[1]["messages"]
    assert retry[1] == {"role": "assistant", "content": "**Pat Owner is waiting on the offer.**"}
    assert "write 'pat owner' as 'you' or 'your'" in retry[2]["content"]


@pytest.mark.asyncio
async def test_a_rewrite_that_still_names_them_is_fixed_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, provider = await _synth(monkeypatch, [
        "**The principal is waiting on the Executive.**",
        "**The principal is still waiting on the Executive.**",
    ])

    assert text == "**You are still waiting on me.**"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_clean_draft_costs_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    text, provider = await _synth(monkeypatch, ["**You're clear today.**"])

    assert text == "**You're clear today.**" and len(provider.calls) == 1


def test_the_today_header_is_written_for_its_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import today
    from openexecutive.briefing import narrative_cache, reader

    monkeypatch.setattr(reader, "principal_reader", lambda: PAT)
    dana = Reader("Dana Lee")
    monkeypatch.setattr(reader, "person_reader", lambda pid: dana if pid == 7 else None)
    viewer: Any = SimpleNamespace(id=7)

    assert today._narrative_reader(narrative_cache.DEFAULT_SCOPE, None) is PAT
    assert today._narrative_reader("person:7", viewer) is dana
    assert today._narrative_reader(narrative_cache.COMPANY_SCOPE, None) is PAT


def test_the_executive_is_i_me_and_my() -> None:
    draft = (
        "You emailed the Executive this morning and the Executive replied.\n"
        "- **Offer** — the Executive's last reply didn't land; the Executive has the figure.\n"
        "The Executive Director role stays open."
    )

    fixed = fix_slips(draft, PAT)

    assert "You emailed me this morning and I replied." in fixed
    assert "- **Offer** — my last reply didn't land; I have the figure." in fixed
    assert "The Executive Director role stays open." in fixed  # a title, left alone
    assert fix_slips("This morning, the Executive replied to Dana.", PAT) == "This morning, I replied to Dana."
    assert slips(fixed, PAT) == []


def test_a_teammates_brief_names_the_owner_and_never_the_principal() -> None:
    dana = Reader("Dana Lee", ("dana@x.com",), owner="Pat Owner")

    fixed = fix_slips("The principal's call is due; Dana Lee has to send the principal a draft.", dana)

    assert fixed == "Pat Owner's call is due; you have to send Pat Owner a draft."
    assert slips("the principal", dana) == ["the principal"]


def test_person_reader_carries_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.briefing import reader
    from openexecutive.people import store

    monkeypatch.setattr(store, "get_person", lambda pid: SimpleNamespace(
        full_name="Dana Lee", email="dana@x.com", email_aliases=[]))
    monkeypatch.setattr(reader, "principal_reader", lambda: PAT)

    assert reader.person_reader(7) == Reader("Dana Lee", ("dana@x.com",), owner="Pat Owner")


def test_the_rewrite_note_gives_each_slip_its_own_replacement() -> None:
    from openexecutive.briefing.reader import rewrite_request

    note = rewrite_request(["pat owner", "the executive"], PAT)
    assert "write 'pat owner' as 'you' or 'your'" in note
    assert "write 'the Executive' as 'I', 'me' or 'my'" in note
    assert "'the executive' as 'you'" not in note

    only_exec = rewrite_request(["the executive"], PAT)
    assert "'you'" not in only_exec.split("(that is you)")[0].replace("as 'I'", "")

    dana = Reader("Dana Lee", owner="Pat Owner")
    assert "write 'the principal' as Pat Owner" in rewrite_request(["the principal"], dana)


@pytest.mark.asyncio
async def test_a_slow_rewrite_keeps_the_first_draft_fixed_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from openexecutive.briefing import narrative

    class _Slow(_Provider):
        async def messages_create(self, **kw: Any) -> object:
            if self.calls:
                await asyncio.sleep(1)
            return await super().messages_create(**kw)

    provider = _Slow(["**Pat Owner is waiting on the offer.**", "never used"])
    monkeypatch.setattr(narrative, "REWRITE_TIMEOUT_S", 0.01)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: provider)
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)

    text = await narrative.synthesize_briefing_narrative(
        today_data={}, activity=[], period_label="p", rendered_context="ctx", reader=PAT,
    )

    assert text == "**You are waiting on the offer.**"


@pytest.mark.asyncio
async def test_an_empty_rewrite_keeps_the_first_draft_fixed_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, provider = await _synth(monkeypatch, ["**Pat Owner is waiting on the offer.**", "  "])

    assert text == "**You are waiting on the offer.**"
    assert len(provider.calls) == 2


def test_the_executive_as_a_noun_phrase_is_left_alone() -> None:
    text = "The Executive team met; the Executive office closed and the Executive replied."

    assert fix_slips(text, PAT) == "The Executive team met; the Executive office closed and I replied."


def test_own_echoes_find_their_chats_and_asks_reported_back() -> None:
    from openexecutive.briefing.narrative import own_echoes

    text = (
        "- **Offer drafts** — both versions are waiting, and the 13:26 alert chat "
        "shows the duplicate-alert noise you asked me to deal with."
    )
    assert own_echoes(text) == ["the 13:26 alert chat", "you asked me"]
    assert own_echoes("As you wanted me to, I moved it after your chat.") == ["you wanted me", "your chat"]
    assert own_echoes("In the chat with me you settled it.") == ["chat with me"]
    # Someone else's chats, and a fact of the day, are not echoes.
    assert own_echoes("Dana wrote in your Google Chat space that the lease is signed.") == []
    assert own_echoes("Your customer conversations with Acme went well.") == []
    assert own_echoes("Your team's chat channel and your sales conversations are quiet.") == []
    assert own_echoes("After your conversation with the board, our conversation rate dropped.") == []
    assert own_echoes("Your chat with Dana settled it.") == []
    assert own_echoes("As our conversation showed, it moved.") == ["our conversation"]
    assert own_echoes("**You're clear today.** I'm muting the duplicate alerts.") == []


@pytest.mark.asyncio
async def test_a_draft_that_reports_their_chat_back_is_rewritten_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, provider = await _synth(monkeypatch, [
        "- **Alerts** — the 13:26 alert chat shows the noise you asked me to deal with.",
        "- **Alerts** — I'm muting the duplicate alerts.",
    ])

    assert text == "- **Alerts** — I'm muting the duplicate alerts."
    assert len(provider.calls) == 2
    note = provider.calls[1]["messages"][2]["content"]
    assert "'the 13:26 alert chat'" in note and "'you asked me'" in note
    assert note.endswith("output only the rewrite.")


@pytest.mark.asyncio
async def test_a_rewrite_that_still_echoes_is_kept_after_one_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, provider = await _synth(monkeypatch, [
        "**You asked me to mute alerts.**",
        "**You asked me to mute alerts; done.**",
    ])

    assert text == "**You asked me to mute alerts; done.**"
    assert len(provider.calls) == 2


def test_settled_mentions_find_items_said_to_need_nothing() -> None:
    from openexecutive.briefing.narrative import drop_settled_points, settled_mentions

    point = (
        "- **Harbor Point** — the buyout is not part of the current offers, "
        "so it stays off the active list for today."
    )
    assert settled_mentions(point) == ["stays off the active list"]
    assert settled_mentions("It was dropped from the list and is no longer on your list.") == [
        "dropped from the list", "no longer on your list",
    ]
    # A bullet that pairs "no longer open" or "needs no action" with a real ask stays.
    mixed = (
        "- **Offer** — the exclusive window is no longer open, so decide by Friday whether to counter.\n"
        "- **Lease** — needs no action from you, but legal needs your sign-off."
    )
    assert settled_mentions(mixed) == []
    assert drop_settled_points(mixed) == mixed
    assert settled_mentions("- **Lease** — the landlord signed, so I'm filing it today.") == []
    text = "**Bottom line.**\n" + point + "\n- **Lease** — signed today.\n**Move today:** file it."
    assert drop_settled_points(text) == "**Bottom line.**\n- **Lease** — signed today.\n**Move today:** file it."


@pytest.mark.asyncio
async def test_a_settled_point_is_rewritten_then_dropped_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, provider = await _synth(monkeypatch, [
        "**Lease signed.**\n- **Harbor Point** — it stays off the list today.",
        "**Lease signed.**\n- **Harbor Point** — it is kept off the list.",
    ])

    assert text == "**Lease signed.**"
    assert len(provider.calls) == 2
    assert "'stays off the list'" in provider.calls[1]["messages"][2]["content"]


@pytest.mark.asyncio
async def test_a_header_of_only_settled_points_is_not_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only = "- **Harbor Point** — it stays off the list today."
    text, provider = await _synth(monkeypatch, [only, only])

    assert text == only
    assert len(provider.calls) == 2
