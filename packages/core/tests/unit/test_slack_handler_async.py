"""Invariants of the async Bolt conversion (#131).

`slack_bot.py` used to be a synchronous Bolt app whose listeners ran on a
10-worker thread pool and drove async code with `asyncio.run()` once per
message. It is now an `AsyncApp` whose listeners await on the application
event loop, because the lifespan starts it in-process and the MCP gateway,
the shared Anthropic client and the SSE queues are all bound to that loop.

That conversion has three failure modes these tests pin down:
  1. a call that became a coroutine but is not awaited — the bot silently
     stops replying;
  2. blocking SQLite/ChromaDB work left inline — it now stalls the whole API,
     not just one Bolt worker;
  3. unbounded concurrency — the async client fans out every envelope.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.integrations import slack_bot

MENTION_EVENT = {
    "text": "<@UBOT> where are we on hiring?",
    "user": "U123",
    "channel": "C1",
    "ts": "1700000000.0",
}


@pytest.fixture(autouse=True)
def slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens for the factory's guard, and no network for its auth_test().

    `create_slack_app()` resolves the bot's own user_id via `auth_test()` at
    construction; stub it so these tests never touch slack.com.
    """
    from openexecutive.config import get_settings

    stub = get_settings().model_copy(
        update={"slack_bot_token": "xoxb-test", "slack_app_token": "xapp-test"}
    )
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: stub)
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient.auth_test",
        AsyncMock(return_value={"user_id": "UBOT"}),
    )
    # create_slack_app() assigns the module global; restore it so these tests
    # cannot leak a resolved id into any other Slack test.
    monkeypatch.setattr(slack_bot, "_bot_user_id", None)


def _person() -> MagicMock:
    p = MagicMock()
    p.id = 7
    p.display_name = "Alex"
    p.full_name = "Alex Rivera"
    # Explicit: a bare MagicMock attribute is truthy, which would silently
    # open the principal-only briefing_context gate in every test.
    p.is_principal = False
    return p


class _FakeSessionStore:
    """In-memory stand-in for memory.session_store.

    The real functions bind `db_path=DB_PATH` as a *default argument* at import
    time, so monkeypatching the module's DB_PATH does nothing — they would
    write to ./episodic_memory.db and leak chat_messages rows into other
    modules' assertions on a full-suite run. Replacing the functions is the
    only isolation that actually holds.
    """

    def __init__(self) -> None:
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.created: dict[str, dict[str, Any]] = {}
        self.timestamped: list[str] = []

    def load_messages(self, session_id: str, **_kw: Any) -> list[dict[str, Any]]:
        return list(self.messages.get(session_id, []))

    def create_session(
        self,
        session_id: str,
        title: str,
        created_at: str,
        caller_person_id: int | None = None,
        **_kw: Any,
    ) -> None:
        self.created.setdefault(
            session_id,
            {
                "title": title,
                "created_at": created_at,
                "caller_person_id": caller_person_id,
            },
        )

    def save_message(
        self, session_id: str, role: str, content: Any, **_kw: Any
    ) -> None:
        self.messages.setdefault(session_id, []).append(
            {"role": role, "content": content}
        )

    def update_session_timestamp(self, session_id: str, **_kw: Any) -> None:
        self.timestamped.append(session_id)


class _Harness:
    """Patches every out-of-process dependency of the Slack handler."""

    def __init__(self) -> None:
        self.say = AsyncMock()
        self.client = MagicMock()
        self.client.conversations_replies = AsyncMock(return_value={"messages": []})
        self.retrieve_threads: list[str] = []
        self.chat = AsyncMock(return_value="the reply")
        self.store = _FakeSessionStore()
        self.episodic_kwargs: list[dict[str, Any]] = []
        self.person = _person()

    def _record_episodic(self, *_: Any, **kwargs: Any) -> str:
        self.episodic_kwargs.append(kwargs)
        return "epi"

    def _record_retrieve(self, *_: Any, **kwargs: Any) -> str:
        self.retrieve_threads.append(threading.current_thread().name)
        self.store_arg = kwargs.get("store")
        return "ctx"

    def __enter__(self) -> _Harness:
        exec_patch = patch("openexecutive.orchestrator.executive.Executive")
        audit_patch = patch("openexecutive.audit.log_event")
        self._patches = [
            patch.object(slack_bot, "_bot_user_id", "UBOT"),
            patch("openexecutive.people.store.find_person_by_slack_id",
                  side_effect=lambda _uid: self.person),
            audit_patch,
            patch("openexecutive.knowledge.retriever.retrieve",
                  side_effect=self._record_retrieve),
            patch("openexecutive.memory.episodic.format_for_prompt",
                  side_effect=self._record_episodic),
            patch("openexecutive.memory.session_store.load_messages",
                  side_effect=self.store.load_messages),
            patch("openexecutive.memory.session_store.create_session",
                  side_effect=self.store.create_session),
            patch("openexecutive.memory.session_store.save_message",
                  side_effect=self.store.save_message),
            patch("openexecutive.memory.session_store.update_session_timestamp",
                  side_effect=self.store.update_session_timestamp),
            patch("openexecutive.onboarding.profile_builder.load_or_create_profile"),
            patch("openexecutive.alerts.pipeline.schedule_evaluation"),
            patch("openexecutive.mcp_server.server.get_store",
                  return_value="WARM_STORE"),
            patch("openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                  new=AsyncMock(return_value=None)),
            exec_patch,
        ]
        for p in self._patches:
            started = p.start()
            if p is exec_patch:
                started.return_value.chat = self.chat
            elif p is audit_patch:
                # Exposed so tests can assert on the rows the handler writes
                # (e.g. the handler_error outcome) without a real DB write.
                self.audit = started
        return self

    def audit_details(self) -> list[dict[str, Any]]:
        """The `details` payload of every audit row this handler wrote."""
        return [
            call.kwargs["details"]
            for call in self.audit.call_args_list
            if "details" in call.kwargs
        ]

    def __exit__(self, *_: Any) -> None:
        for p in reversed(self._patches):
            p.stop()


@asynccontextmanager
async def _listeners() -> AsyncIterator[dict[str, Any]]:
    """Build the real async Bolt app and hand back its event listeners.

    Closes the socket-mode client on the way out so the suite does not leak
    an aiohttp session per test.
    """
    app, handler = await slack_bot.create_slack_app()
    try:
        yield {
            listener.ack_function.__name__: listener.ack_function
            for listener in app._async_listeners
        }
    finally:
        with suppress(Exception):
            await handler.close_async()
        session = getattr(app.client, "session", None)
        if session is not None and not session.closed:
            await session.close()


@pytest.mark.asyncio
async def test_mention_replies_with_no_unawaited_coroutine() -> None:
    """The whole handler path runs and replies exactly once.

    The real guard against the classic sync->async miss is `AsyncMock`, which
    distinguishes *called* from *awaited*: a coroutine that is created but
    never awaited leaves `assert_awaited_once()` failing. (Promoting
    RuntimeWarning to an error does NOT catch it — "coroutine was never
    awaited" is raised from `__del__` during GC, where Python prints
    "Exception ignored in" rather than failing the test.)

    These tests call the listener directly, so Bolt's middleware and kwarg
    injection are not exercised; `test_slack_lifespan.py` covers the wiring.
    """
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_mention"](
                event=dict(MENTION_EVENT), say=h.say, client=h.client
            )

            h.chat.assert_awaited_once()
            assert h.say.await_count == 1
            assert h.say.await_args.kwargs["text"] == "the reply"


@pytest.mark.asyncio
async def test_blocking_retrieval_runs_off_the_event_loop() -> None:
    """ChromaDB retrieval must not run inline on the application loop.

    `retrieve()` embeds the query on CPU and can take seconds; inline it would
    stall every other request in the process (`/chat`, `/health`, the
    scheduler, the Discord bot). api/routes/chat.py offloads the same call.
    """
    main_thread = threading.current_thread().name
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_mention"](
                event=dict(MENTION_EVENT), say=h.say, client=h.client
            )

    assert h.retrieve_threads, "retrieve() was never called"
    assert main_thread not in h.retrieve_threads, (
        "retrieve() ran on the event-loop thread — it must be offloaded "
        f"(threads seen: {h.retrieve_threads})"
    )


@pytest.mark.asyncio
async def test_retrieval_reuses_the_warm_store() -> None:
    """Passing the lifespan's store avoids building a ChromaDB client per message."""
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_mention"](
                event=dict(MENTION_EVENT), say=h.say, client=h.client
            )

    assert h.store_arg == "WARM_STORE"


@pytest.mark.asyncio
async def test_inbound_handling_is_concurrency_bounded() -> None:
    """The async client caps nothing; the adapter must cap itself.

    The sync adapter was bounded by Bolt's listener_executor
    (ThreadPoolExecutor(max_workers=5)), so a burst of Slack traffic could
    never fan out into more than 5 concurrent `executive.chat()` calls. The
    async client caps nothing, so the adapter has to cap itself.
    """
    peak = 0
    current = 0

    async def _slow_chat(**_: Any) -> str:
        nonlocal peak, current
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1
        return "the reply"

    async with _listeners() as listeners:
        with _Harness() as h:
            h.chat.side_effect = _slow_chat
            await asyncio.gather(*(
                listeners["handle_mention"](
                    event=dict(MENTION_EVENT), say=h.say, client=h.client
                )
                for _ in range(25)
            ))

    assert h.say.await_count == 25, "some messages were dropped"
    assert peak <= slack_bot._MAX_CONCURRENT_HANDLERS, (
        f"{peak} handlers ran concurrently, bound is "
        f"{slack_bot._MAX_CONCURRENT_HANDLERS}"
    )


# --------------------------------------------------------------------- #
# Error path (#136): the generic apology must leave a trail
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handler_error_is_audited_with_a_correlated_session_id() -> None:
    """A failed turn wrote one uncorrelated log line and nothing else.

    /audit then showed an `integration_inbound` row with no matching turn and
    no error marker, so "I encountered an error processing your request" was
    undiagnosable after the fact (#136). The failure must land in the audit
    trail, tagged `handler_error`, under the same session_id as the inbound.
    """
    async with _listeners() as listeners:
        with _Harness() as h:
            h.chat.side_effect = RuntimeError("provider exploded")
            await listeners["handle_mention"](
                event=dict(MENTION_EVENT), say=h.say, client=h.client
            )

            # The user still gets the apology.
            assert h.say.await_count == 1
            assert "encountered an error" in h.say.await_args.kwargs["text"]

            errors = [
                d for d in h.audit_details()
                if d.get("outcome") == "handler_error"
            ]
            assert len(errors) == 1, "the failure was not audited"
            assert errors[0]["slack_user"] == MENTION_EVENT["user"]
            assert errors[0]["mode"] == "mention"
            assert "RuntimeError" in errors[0]["error"]

            # Correlated: same session_id as the inbound row, so the two join.
            session_ids = {
                call.kwargs.get("session_id")
                for call in h.audit.call_args_list
                if "details" in call.kwargs
            }
            assert len(session_ids) == 1
            assert next(iter(session_ids))


@pytest.mark.asyncio
async def test_failing_error_reply_does_not_escape_the_listener() -> None:
    """If `say` itself fails inside the except block, the exception used to
    propagate into Bolt. Telegram and Google Chat already guard this."""
    async with _listeners() as listeners:
        with _Harness() as h:
            h.chat.side_effect = RuntimeError("provider exploded")
            h.say.side_effect = RuntimeError("slack is down")

            # Must not raise.
            await listeners["handle_mention"](
                event=dict(MENTION_EVENT), say=h.say, client=h.client
            )

            assert h.say.await_count == 1


# --------------------------------------------------------------------- #
# Conversational state (#136)
# --------------------------------------------------------------------- #

DM_EVENT = {
    "text": "run my morning brief as a test",
    "user": "U123",
    "channel": "D9",
    "channel_type": "im",
    "ts": "1700000100.0",
}


def test_session_id_is_per_conversation_not_per_message() -> None:
    """The old scheme was `slack:{channel}:{thread_ts or ts}`.

    In a DM there is no thread_ts, so every message minted a fresh id — which
    would leave history permanently empty even after persistence was added.
    """
    dm_first = slack_bot._slack_session_id(
        mode="dm", channel="D9", user_id="U123",
        thread_ts="1700000100.0", is_threaded_reply=False,
    )
    dm_second = slack_bot._slack_session_id(
        mode="dm", channel="D9", user_id="U123",
        thread_ts="1700000200.0", is_threaded_reply=False,
    )
    assert dm_first == dm_second == "slack:dm:U123"

    assert slack_bot._slack_session_id(
        mode="thread_continuation", channel="C1", user_id="U123",
        thread_ts="1700000000.0", is_threaded_reply=True,
    ) == "slack:thread:C1:1700000000.0"

    # A mention that starts a thread rolls per (channel, person).
    assert slack_bot._slack_session_id(
        mode="mention", channel="C1", user_id="U123",
        thread_ts="1700000000.0", is_threaded_reply=False,
    ) == "slack:channel:C1:U123"


@pytest.mark.asyncio
async def test_second_dm_turn_sees_the_first_turns_history() -> None:
    """The reported failure, end to end.

    OE asks "should I send it to Slack?", the user replies "yes" — and the
    second turn must carry the first turn's Q+A. On main this fails twice
    over: nothing was persisted, and each DM had its own session id anyway.
    """
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_message"](
                event=dict(DM_EVENT), say=h.say, client=h.client
            )
            second = dict(DM_EVENT, text="yes, send it to Slack", ts="1700000200.0")
            await listeners["handle_message"](
                event=second, say=h.say, client=h.client
            )

            assert h.chat.await_count == 2
            replayed = h.chat.await_args_list[1].kwargs["session"].conversation_history
            assert [m["role"] for m in replayed] == ["user", "assistant"]
            assert replayed[0]["content"] == "run my morning brief as a test"
            assert replayed[1]["content"] == "the reply"

            # One rolling session for the DM, owned by the sender.
            assert list(h.store.created) == ["slack:dm:U123"]
            assert h.store.created["slack:dm:U123"]["caller_person_id"] == 7


@pytest.mark.asyncio
async def test_episodic_memory_is_scoped_to_the_session() -> None:
    """An unscoped format_for_prompt() mixes every other conversation's
    episodes into this turn. discord_bot already passes session_id."""
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_message"](
                event=dict(DM_EVENT), say=h.say, client=h.client
            )

    assert h.episodic_kwargs == [{"session_id": "slack:dm:U123"}]


@pytest.mark.asyncio
async def test_nothing_is_persisted_when_the_turn_fails() -> None:
    """A failed turn must not leave a half-written exchange behind."""
    async with _listeners() as listeners:
        with _Harness() as h:
            h.chat.side_effect = RuntimeError("provider exploded")
            await listeners["handle_message"](
                event=dict(DM_EVENT), say=h.say, client=h.client
            )

            assert h.store.messages == {}
            assert h.store.created == {}


@pytest.mark.asyncio
async def test_channel_mention_double_writes_to_the_thread_it_roots() -> None:
    """The bot's reply to a channel mention roots a thread at that message's
    ts. Without the double-write, the first continuation inside that thread
    would start cold — exactly the "@oe ...?" / "yes" failure."""
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_mention"](
                event=dict(MENTION_EVENT), say=h.say, client=h.client
            )

    assert set(h.store.created) == {
        "slack:channel:C1:U123",
        f"slack:thread:C1:{MENTION_EVENT['ts']}",
    }
    for sid in h.store.created:
        assert [m["role"] for m in h.store.messages[sid]] == ["user", "assistant"]
    # Multi-human surface: the persisted user turn is attributed.
    channel_turn = h.store.messages["slack:channel:C1:U123"][0]
    assert channel_turn["content"].startswith("[Alex]: ")


@pytest.mark.asyncio
async def test_same_conversation_turns_are_serialized() -> None:
    """Two messages in one conversation must not both read history before
    either writes it. The inflight semaphore caps fan-out, not interleaving."""
    peak = 0
    current = 0

    async def _slow_chat(**_: Any) -> str:
        nonlocal peak, current
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1
        return "the reply"

    async with _listeners() as listeners:
        with _Harness() as h:
            h.chat.side_effect = _slow_chat
            await asyncio.gather(*(
                listeners["handle_message"](
                    event=dict(DM_EVENT, ts=f"17000001{i:02d}.0"),
                    say=h.say,
                    client=h.client,
                )
                for i in range(4)
            ))

    assert h.say.await_count == 4
    assert peak == 1, f"{peak} turns ran concurrently in one conversation"


# --------------------------------------------------------------------- #
# Honest capability statements (#136)
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_principal_dm_gets_the_open_alert_digest() -> None:
    """Slack was never passed briefing_context, so the Executive had no way
    to see the open board — it could neither enumerate it accurately nor cite
    a trustworthy alert_id."""
    async with _listeners() as listeners:
        with _Harness() as h:
            h.person.is_principal = True
            with patch(
                "openexecutive.briefing.context.format_open_alerts_for_prompt",
                return_value="[42] (action) Renew the vendor contract",
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

    assert "[42]" in h.chat.await_args.kwargs["briefing_context"]


@pytest.mark.asyncio
async def test_public_channel_never_receives_the_open_alert_digest() -> None:
    """The board is company-wide. Pulling it into a shared channel would leak
    every open item to everyone in that channel."""
    async with _listeners() as listeners:
        with _Harness() as h:
            h.person.is_principal = True
            with patch(
                "openexecutive.briefing.context.format_open_alerts_for_prompt",
                return_value="[42] (action) Renew the vendor contract",
            ):
                await listeners["handle_mention"](
                    event=dict(MENTION_EVENT), say=h.say, client=h.client
                )

    assert h.chat.await_args.kwargs["briefing_context"] == ""


@pytest.mark.asyncio
async def test_non_principal_dm_never_receives_the_open_alert_digest() -> None:
    async with _listeners() as listeners:
        with _Harness() as h:
            h.person.is_principal = False
            with patch(
                "openexecutive.briefing.context.format_open_alerts_for_prompt",
                return_value="[42] (action) Renew the vendor contract",
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

    assert h.chat.await_args.kwargs["briefing_context"] == ""


@pytest.mark.asyncio
async def test_turn_carries_a_channel_context_block_naming_slack() -> None:
    """Without this the model has no idea it is on Slack rather than in the
    web app, and invites confirmations it cannot act on (#136)."""
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_message"](
                event=dict(DM_EVENT), say=h.say, client=h.client
            )

    block = h.chat.await_args.kwargs["channel_context_block"]
    assert "Slack" in block
    assert "briefing page" in block


# --------------------------------------------------------------------- #
# Approval reachability (#136)
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dm_does_not_pass_its_own_ts_as_a_reply_reference() -> None:
    """`thread_ts` falls back to the message's own ts, and passing that made
    the resolver treat every Slack message as an explicit reference to
    nothing — short-circuiting tiers 2 and 3, so no approval could land."""
    async with _listeners() as listeners:
        with _Harness() as h:
            resolve = AsyncMock(return_value=None)
            with patch(
                "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                resolve,
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

    kwargs = resolve.await_args.kwargs
    assert kwargs["in_reply_to"] == ""
    assert kwargs["session_ids"] == ["slack:dm:U123"]
    assert kwargs["channel"] == "slack"


@pytest.mark.asyncio
async def test_threaded_reply_still_passes_a_real_reply_reference() -> None:
    """Inside a thread, thread_ts IS a meaningful reference — tier 1 should
    still get to use it."""
    event = {
        "text": "approved",
        "user": "U123",
        "channel": "C1",
        "ts": "1700000500.0",
        "thread_ts": "1700000000.0",
    }
    async with _listeners() as listeners:
        with _Harness() as h:
            h.client.conversations_replies = AsyncMock(
                return_value={
                    "messages": [
                        {"user": "U123", "text": "<@UBOT> run the brief"},
                        {"user": "UBOT", "text": "shall I?"},
                    ]
                }
            )
            resolve = AsyncMock(return_value=None)
            with patch(
                "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                resolve,
            ):
                await listeners["handle_message"](
                    event=event, say=h.say, client=h.client
                )

    kwargs = resolve.await_args.kwargs
    assert kwargs["in_reply_to"] == "1700000000.0"
    assert kwargs["session_ids"] == [
        "slack:thread:C1:1700000000.0",
        "slack:channel:C1:U123",
    ]


@pytest.mark.asyncio
async def test_resolved_gate_acknowledgement_does_not_overstate_what_happened() -> None:
    """Resolving a gate records the decision and closes the sign-off — it does
    NOT resume the workflow (generator resume is unimplemented). The old
    "Got it — your response has been recorded." implied the work was now in
    flight."""
    from openexecutive.workflows.wait_for_human import WaitForHumanResolution

    resolution = WaitForHumanResolution(
        run_id="run-1",
        reply_text="yes, go ahead",
        source_channel="slack",
        parsed_decision={"decision": "approve", "note": ""},
        person_id=7,
    )

    async with _listeners() as listeners:
        with _Harness() as h:
            with (
                patch(
                    "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                    new=AsyncMock(return_value=resolution),
                ),
                patch(
                    "openexecutive.workflows.resumer.apply_resolution",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "openexecutive.workflows.resumer.resolution_acknowledgement",
                    return_value="Approved — Vendor Review. That's recorded.",
                ),
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

            # The gate answered; the message never reached the chat path.
            h.chat.assert_not_awaited()
            assert h.say.await_args.kwargs["text"].startswith("Approved — Vendor Review")


@pytest.mark.asyncio
async def test_unresolved_message_still_reaches_the_chat_path() -> None:
    """With an open gate, every message from that person hits the resolver.
    One that isn't an answer must fall through, not be swallowed."""
    async with _listeners() as listeners:
        with _Harness() as h:
            with patch(
                "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                new=AsyncMock(return_value=None),
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

            h.chat.assert_awaited_once()
            assert h.say.await_args.kwargs["text"] == "the reply"


@pytest.mark.asyncio
async def test_session_declares_its_origin_channel() -> None:
    """A gate raised mid-turn needs to know where the answer will come from."""
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_message"](
                event=dict(DM_EVENT), say=h.say, client=h.client
            )

    session = h.chat.await_args.kwargs["session"]
    assert session.origin_channel == "slack"
    assert session.origin_channel_ref == "U123"
    assert session.caller_person_id == 7


@pytest.mark.asyncio
async def test_thread_reply_offers_the_parent_channel_session_too() -> None:
    """A channel mention raises its gate under `slack:channel:{c}:{u}`, but
    the bot's reply roots a thread — so the answer arrives under the THREAD
    id. Offering only that id left the gate permanently unanswerable, which
    is #136 one surface over."""
    event = {
        "text": "approved",
        "user": "U123",
        "channel": "C1",
        "ts": "1700000500.0",
        "thread_ts": "1700000000.0",
    }
    async with _listeners() as listeners:
        with _Harness() as h:
            h.client.conversations_replies = AsyncMock(
                return_value={
                    "messages": [
                        {"user": "U123", "text": "<@UBOT> run the brief"},
                        {"user": "UBOT", "text": "shall I?"},
                    ]
                }
            )
            resolve = AsyncMock(return_value=None)
            with patch(
                "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                resolve,
            ):
                await listeners["handle_message"](
                    event=event, say=h.say, client=h.client
                )

    ids = resolve.await_args.kwargs["session_ids"]
    assert "slack:thread:C1:1700000000.0" in ids
    assert "slack:channel:C1:U123" in ids


@pytest.mark.asyncio
async def test_dm_offers_only_its_own_session() -> None:
    """The channel alias must not leak into DMs — there is no parent there."""
    async with _listeners() as listeners:
        with _Harness() as h:
            resolve = AsyncMock(return_value=None)
            with patch(
                "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                resolve,
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

    assert resolve.await_args.kwargs["session_ids"] == ["slack:dm:U123"]


@pytest.mark.asyncio
async def test_at_mention_inside_a_dm_is_handled_once() -> None:
    """Slack fires BOTH `message` (im) and `app_mention` for an @-mention in a
    DM. Handling both meant two replies and two different session ids, forking
    the conversation's history."""
    event = dict(DM_EVENT, text="<@UBOT> approve the budget")
    async with _listeners() as listeners:
        with _Harness() as h:
            await listeners["handle_message"](
                event=dict(event), say=h.say, client=h.client
            )
            await listeners["handle_mention"](
                event=dict(event), say=h.say, client=h.client
            )

            assert h.chat.await_count == 1
            assert h.say.await_count == 1
            assert list(h.store.created) == ["slack:dm:U123"]


@pytest.mark.asyncio
async def test_session_records_the_alert_ids_the_briefing_block_named() -> None:
    """Server-side counterpart to ack_alert's trust rule: only ids the server
    derived from the live board can be acked from the channel.

    The digest reports that set through `trusted_ids` (the whole live board),
    not merely the subset it printed — a card past the render cap is
    still on the page and must stay ackable.
    """
    async with _listeners() as listeners:
        with _Harness() as h:
            h.person.is_principal = True

            def _fake_digest(trusted_ids: list[int] | None = None, **_kw: Any) -> str:
                if trusted_ids is not None:
                    trusted_ids.extend([11, 12])
                return "[11] (action) A\n[12] (action) B"

            with patch(
                "openexecutive.briefing.context.format_open_alerts_for_prompt",
                side_effect=_fake_digest,
            ):
                await listeners["handle_message"](
                    event=dict(DM_EVENT), say=h.say, client=h.client
                )

    assert h.chat.await_args.kwargs["session"].trusted_alert_ids == {11, 12}


@pytest.mark.asyncio
async def test_a_turn_without_a_briefing_block_trusts_no_alert_ids() -> None:
    async with _listeners() as listeners:
        with _Harness() as h:
            h.person.is_principal = False
            await listeners["handle_message"](
                event=dict(DM_EVENT), say=h.say, client=h.client
            )

    assert h.chat.await_args.kwargs["session"].trusted_alert_ids == set()


@pytest.mark.asyncio
async def test_no_alias_for_a_thread_someone_else_rooted() -> None:
    """Widening on "threaded and not a DM" alone offered Alice's channel
    session inside ANY thread in that channel — including one rooted by Bob's
    mention. A remark Alice made to Bob could then resolve her open gate,
    post the run's title into Bob's thread, and do it having skipped the
    multi-human response gate, which runs after the resolver."""
    event = {
        "text": "yeah that works for me",
        "user": "U123",
        "channel": "C1",
        "ts": "1700000500.0",
        "thread_ts": "1700000000.0",
    }
    async with _listeners() as listeners:
        with _Harness() as h:
            h.client.conversations_replies = AsyncMock(
                return_value={
                    "messages": [
                        {"user": "UBOB", "text": "<@UBOT> what's our runway?"},
                        {"user": "UBOT", "text": "about 14 months"},
                    ]
                }
            )
            resolve = AsyncMock(return_value=None)
            with patch(
                "openexecutive.workflows.inbound_resolver.resolve_inbound_message",
                resolve,
            ):
                await listeners["handle_message"](
                    event=event, say=h.say, client=h.client
                )

    assert resolve.await_args.kwargs["session_ids"] == [
        "slack:thread:C1:1700000000.0"
    ]
