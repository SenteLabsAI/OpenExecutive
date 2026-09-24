from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from openexecutive.audit.redaction import ERROR_DETAIL_LEN

logger = logging.getLogger(__name__)

# Cached at startup via client.auth_test(). Used to:
#  1. Detect when the bot is the author of a message in a thread (so we
#     can recognize "the bot has already engaged" without separately
#     tracking session state).
#  2. Suppress double-firing on the generic `message` event when the
#     message is actually an @-mention (which `app_mention` handles).
# Stays None if auth_test fails at startup — handler falls back to the
# legacy mention/DM-only behavior so the bot still responds, just
# without thread auto-continuation.
_bot_user_id: str | None = None

# Preserves the concurrency ceiling the sync adapter had. That ceiling was
# Bolt's own listener_executor — ThreadPoolExecutor(max_workers=5)
# (slack_bolt/app/app.py) — which ran the handler bodies. The socket client's
# `concurrency=10` pool only did dispatch+ack, which returns immediately, so
# 5 (not 10) was the real cap on concurrent executive.chat() calls.
_MAX_CONCURRENT_HANDLERS = 5

# Serializes turns within one conversation. The semaphore above caps how many
# Slack messages are in flight across the workspace; it does nothing to stop
# two messages in the SAME conversation from being processed concurrently,
# which makes both turns read history before either writes it — interleaved
# turns and a confused reply. Mirrors the per-chat lock in telegram_bot.py and
# the per-session lock in discord_bot.py. Unbounded growth matches both of
# those; a conversation's lock is a few dozen bytes.
_session_locks: dict[str, asyncio.Lock] = {}


def _session_lock(session_id: str) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def _slack_session_id(
    *,
    mode: str,
    channel: str,
    user_id: str,
    thread_ts: str,
    is_threaded_reply: bool,
) -> str:
    """Deterministic session id keyed to the conversation surface.

    The previous scheme was ``slack:{channel}:{thread_ts}`` with ``thread_ts``
    falling back to the message's own ``ts``. In a DM there is no ``thread_ts``,
    so every single message minted a fresh id — which, once history was
    persisted against it, would still have left every Slack turn starting from
    zero. That is the root cause of #136: the bot asked "should I send it to
    Slack?", the user said yes, and the next turn had no record of the question.

    - DM → ``slack:dm:{user_id}`` — one long-lived conversation per person,
      matching ``discord:dm:{user_id}``.
    - Reply inside an existing thread → ``slack:thread:{channel}:{thread_ts}``.
    - Mention that starts a thread → ``slack:channel:{channel}:{user_id}``, a
      rolling per-(channel, person) session. The reply also roots a Slack
      thread at this message's ``ts``, so the turn is double-written to
      ``slack:thread:{channel}:{ts}`` (see `_persist_turn`) — otherwise the
      first continuation inside that thread would start cold.
    """
    if mode == "dm":
        return f"slack:dm:{user_id}"
    if is_threaded_reply:
        return f"slack:thread:{channel}:{thread_ts}"
    return f"slack:channel:{channel}:{user_id}"


def _format_user_content(text: str, speaker: str | None) -> str:
    """Prefix a persisted user turn with its speaker in multi-human threads.

    Replayed history is otherwise unattributed: in a thread with three people
    the model cannot tell who said what. Mirrors discord_bot's
    `_format_user_content`; DMs stay unprefixed (1:1, no ambiguity).
    """
    if not speaker:
        return text
    return f"[{speaker}]: {text}"


def _session_title(person: object, mode: str) -> str:
    """Human-readable title for the /sessions sidebar."""
    name = (
        getattr(person, "display_name", None)
        or getattr(person, "full_name", None)
        or "Slack"
    )
    surface = {"dm": "DM", "mention": "channel", "thread_continuation": "thread"}
    return f"Slack {surface.get(mode, mode)} — {name}"


def _persist_turn(
    *,
    session_id: str,
    title: str,
    created_at: str,
    owner_person_id: int | None,
    user_text: str,
    assistant_text: str,
    also_session_id: str | None = None,
    sender_person_id: int | None = None,
) -> None:
    """Write one Q+A to the session store, best-effort.

    Sync (SQLite) — call it via ``asyncio.to_thread``. Never raises: the user
    already has their reply, so a persistence failure must not turn a
    successful turn into the generic error path. ``also_session_id`` is the
    double-write target for a mention that roots a new Slack thread.
    """
    from openexecutive.memory.session_store import (
        create_session,
        save_message,
        update_session_timestamp,
    )

    for sid in (session_id, also_session_id):
        if not sid:
            continue
        try:
            create_session(sid, title, created_at, caller_person_id=owner_person_id)
            save_message(sid, "user", user_text, sender_person_id=sender_person_id)
            save_message(sid, "assistant", assistant_text)
            update_session_timestamp(sid)
        except Exception:
            logger.exception("Slack: failed to persist turn for session %s", sid)


def _thread_root_author(thread_replies: list[dict] | None) -> str:
    """The user_id who posted the message a thread hangs off, if known."""
    if not thread_replies:
        return ""
    return str(thread_replies[0].get("user") or "")


def _session_id_aliases(
    *,
    mode: str,
    channel: str,
    user_id: str,
    session_id: str,
    is_threaded_reply: bool,
    thread_replies: list[dict] | None,
) -> list[str]:
    """Every session id this inbound message legitimately belongs to.

    Normally just its own. The exception is a reply inside a thread THIS
    person rooted: the bot answers an @mention in a channel by replying in a
    thread hung off that mention, so the conversation also lives under the
    rolling `slack:channel:{channel}:{user}` session — the same pair
    `_persist_turn` double-writes history to. Without the parent id, a gate
    raised on the mention can never be answered in the thread it was asked in.

    The root-author check is what keeps that from being a hole. Widening on
    "threaded and not a DM" alone would offer Alice's channel session inside
    ANY thread in that channel — including one rooted by Bob's mention — so a
    remark Alice made to Bob could resolve her open gate, post the run's title
    into Bob's thread, and do it having skipped the multi-human response gate
    (the resolver runs before it). Scoped to threads this person started, the
    alias only ever names a conversation that is genuinely theirs.

    A thread whose replies could not be fetched degrades to no alias: the
    gate stays unanswered, which is recoverable, rather than over-matching.
    """
    aliases = [session_id]
    if (
        mode != "dm"
        and is_threaded_reply
        and channel
        and user_id
        and _thread_root_author(thread_replies) == user_id
    ):
        aliases.append(f"slack:channel:{channel}:{user_id}")
    return aliases


def _replies_contain_bot_message(messages: list[dict], bot_user_id: str | None) -> bool:
    """True iff any message in the thread was posted by this bot.

    `bot_user_id` is the cached slack user_id from auth_test(). If we
    couldn't resolve it at startup, return False — that disables thread
    auto-continuation but keeps the rest of the integration working.
    """
    if not bot_user_id:
        return False
    return any(str(m.get("user") or "") == bot_user_id for m in messages)


def _count_distinct_humans(
    messages: list[dict],
    current_user_id: str | None,
    bot_user_id: str | None,
) -> int:
    """Count distinct non-bot human user_ids across the thread history.

    Used for the gate's single-human bypass: if only one human has ever
    spoken in this thread, every message is implicitly addressed to the
    bot and running the gate just risks false negatives.
    """
    humans: set[str] = set()
    for m in messages:
        uid = str(m.get("user") or "")
        if not uid or uid == (bot_user_id or ""):
            continue
        humans.add(uid)
    if current_user_id:
        humans.add(str(current_user_id))
    return len(humans)


def _replies_to_gate_history(
    messages: list[dict], bot_user_id: str | None
) -> list[dict]:
    """Convert raw Slack ``conversations_replies`` messages into the
    ``[{'role': 'user'|'assistant', 'content': str}]`` shape the shared
    response gate expects.

    Each human turn is prefixed with ``[user_id]: `` so the gate model can
    tell speakers apart even when we don't have a clean display name in
    the payload. The bot's own messages become ``assistant`` turns.
    Subtype messages (channel joins, edits, etc.) and messages with no
    text are dropped — the gate doesn't need them.
    """
    out: list[dict] = []
    for m in messages:
        if m.get("subtype"):
            continue
        text = m.get("text") or ""
        if not text.strip():
            continue
        uid = str(m.get("user") or "")
        if bot_user_id and uid == bot_user_id:
            out.append({"role": "assistant", "content": text})
        elif uid:
            out.append({"role": "user", "content": f"[{uid}]: {text}"})
    return out


async def create_slack_app():
    """Build the async Bolt app and its Socket Mode handler.

    Async Bolt (not the sync ``App``) so every handler awaits on the caller's
    event loop. That matters because the app is started from the FastAPI
    lifespan: the MCP gateway's anyio task group and stdio subprocess, the
    shared ``AsyncAnthropic`` client and the SSE subscriber queues are all
    bound to the uvicorn loop, and driving them from a throwaway
    ``asyncio.run`` loop on a Bolt worker thread would corrupt or hang them.
    Every other inbound adapter (discord, telegram, google_chat) already
    awaits on the request loop; this brings Slack in line.

    Imports stay deferred inside the factory so the module imports cleanly
    where slack_bolt is absent — the helper unit tests rely on that.
    """
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    from openexecutive.config import get_settings

    settings = get_settings()

    if not settings.slack_bot_token or not settings.slack_app_token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set to run the Slack bot"
        )

    app = AsyncApp(token=settings.slack_bot_token)

    # Resolve and cache the bot's own user_id once at startup. Failure
    # here disables thread auto-continuation but does NOT prevent the
    # rest of the bot from running — the mention and DM paths don't
    # depend on this value.
    global _bot_user_id
    try:
        auth = await app.client.auth_test()
        _bot_user_id = str(auth.get("user_id") or "") or None
        logger.info("Slack bot_user_id resolved to %s", _bot_user_id)
    except Exception:
        logger.exception(
            "Slack: auth_test() failed at startup — "
            "thread auto-continuation disabled"
        )
        _bot_user_id = None

    # The async client ensure_future()s every inbound envelope with no cap,
    # so without this a burst of Slack traffic fans out into unbounded
    # concurrent executive.chat() calls. The sync adapter was bounded (see
    # _MAX_CONCURRENT_HANDLERS); keep it bounded.
    # Created here (not at module scope) so it binds to the loop that runs it.
    inflight = asyncio.Semaphore(_MAX_CONCURRENT_HANDLERS)

    def _clean_message(text: str) -> str:
        text = re.sub(r"<@\w+>", "", text)
        return text.strip()

    async def _handle_message(
        event: dict, say, client=None, mode: str = "mention"
    ) -> None:
        """Process one inbound Slack message.

        ``mode`` is set by the caller:
          - ``"mention"`` — fired from ``app_mention``. Unconditional reply.
          - ``"dm"`` — direct message. Unconditional reply.
          - ``"thread_continuation"`` — non-mention message in a thread
            the bot has previously replied in. Gated via
            ``response_gate.should_respond``; skipped if the gate says NO
            (with an audit row capturing the reason).
        """
        text = event.get("text", "")
        cleaned = _clean_message(text)
        if not cleaned:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        slack_user_id = event.get("user", "")

        # Whether this message is a reply *inside* an existing thread, as
        # opposed to a thread starter or a DM. Used for three things: whether
        # to fetch thread replies, which session id to use, and whether
        # `thread_ts` is a meaningful reply reference for the inbound resolver.
        is_threaded_reply = (
            thread_ts is not None
            and str(thread_ts) != str(event.get("ts") or "")
        )
        # Deterministic per-conversation session id. Every audit row from this
        # inbound (chat_turn, specialist_consult, tool_invocation) shares it
        # with the integration_inbound row, and it is the key the stored
        # conversation history hangs off.
        session_id = _slack_session_id(
            mode=mode,
            channel=str(event.get("channel", "")),
            user_id=str(slack_user_id),
            thread_ts=str(thread_ts or ""),
            is_threaded_reply=is_threaded_reply,
        )

        # Fetch thread replies ONCE up front so we can use the result for
        # (a) the "has the bot engaged?" check on thread_continuation mode,
        # (b) the single-human bypass on the response gate, and
        # (c) the multi-peer co-presence enumeration further down.
        # Standalone messages and DMs are 1:1 — skip the API call.
        can_fetch_replies = client is not None and is_threaded_reply
        thread_replies: list[dict] | None = None
        if can_fetch_replies:
            try:
                # 5s bound: this call sits on the user-facing TTFB path (it
                # runs before executive.chat). Enforced with wait_for, not the
                # `timeout=` kwarg — AsyncWebClient has no such parameter, so
                # that value was silently forwarded as a query string and the
                # real ceiling stayed the client default of 30s. A timeout is
                # caught below and degrades to thread_replies=None — which
                # for mode="thread_continuation" trips the bot-presence guard
                # and drops the message. That is the intended trade: a slow
                # Slack API should cost one missed continuation, not a stalled
                # event loop. Mentions and DMs are unaffected (they do not
                # depend on thread_replies to decide whether to reply).
                replies_resp = await asyncio.wait_for(
                    client.conversations_replies(
                        channel=event.get("channel", ""),
                        ts=str(thread_ts),
                        limit=200,
                    ),
                    timeout=5,
                )
                thread_replies = replies_resp.get("messages", []) or []
            except Exception:
                logger.warning(
                    "Slack: conversations_replies failed for thread %s — "
                    "passing empty co-present list",
                    thread_ts,
                    exc_info=True,
                )
                thread_replies = None

        # Bot-presence guard for thread_continuation mode. If the bot
        # hasn't actually replied in this thread, silently drop — never
        # audit, never trigger alerts, never run the gate. This is what
        # keeps the new behavior from spamming /audit with every random
        # thread message in every channel the bot is in.
        if mode == "thread_continuation" and (
            thread_replies is None
            or not _replies_contain_bot_message(thread_replies, _bot_user_id)
        ):
            return

        # Audit writes go off-loop too: log_event opens SQLite with a 5s
        # busy timeout, and the scheduler, resumer and email poller all write
        # the same DB — under contention an inline call would stall the whole
        # application event loop for that long.
        from openexecutive.audit import log_event as audit_log
        await asyncio.to_thread(
            audit_log,
            "integration_inbound",
            f"Inbound slack from user={slack_user_id} channel={event.get('channel', '')}: {cleaned[:160]}",
            actor="slack",
            session_id=session_id,
            details={
                "channel": "slack",
                "slack_channel": event.get("channel"),
                "slack_user": slack_user_id,
                "ts": event.get("ts"),
                "thread_ts": thread_ts,
                "text_len": len(cleaned),
                "mode": mode,
            },
        )

        # Roster gate. Slack has no env allowlist — the People roster is
        # the only access control. Drop messages from any Slack user
        # without a matching slack_user_id on a non-archived Person row.
        from openexecutive.people.store import find_person_by_slack_id
        sender_person = (
            await asyncio.to_thread(find_person_by_slack_id, slack_user_id)
            if slack_user_id
            else None
        )
        if sender_person is None:
            await asyncio.to_thread(
                audit_log,
                "integration_inbound",
                f"Rejected: slack user={slack_user_id} not in People roster",
                actor="slack",
                session_id=session_id,
                details={
                    "channel": "slack",
                    "slack_user": slack_user_id,
                    "outcome": "rejected_unknown_sender",
                },
            )
            return

        # WaitForHuman inbound resolver — check BEFORE alert triage.
        # If this message answers an awaiting workflow run, skip triage.
        if sender_person.id is not None:
            from openexecutive.workflows.inbound_resolver import (
                resolve_and_acknowledge,
            )

            async def _say_ack(text: str) -> None:
                await say(text=text, thread_ts=thread_ts)

            if await resolve_and_acknowledge(
                channel="slack",
                channel_ref=slack_user_id,
                person_id=sender_person.id,
                text=cleaned,
                send=_say_ack,
                message_id=str(event.get("ts") or ""),
                # Only a reply INSIDE a thread carries a meaningful reply
                # reference. `thread_ts` falls back to this message's own ts,
                # and passing that made the resolver treat every Slack message
                # as an explicit reference to nothing — which short-circuited
                # tiers 2 and 3 and made Slack approvals impossible (#136).
                in_reply_to=str(thread_ts) if is_threaded_reply else "",
                # Both ids this conversation is reachable under. A mention in
                # a channel raises its gate under the rolling channel session,
                # but the bot's reply roots a thread — so the answer arrives
                # under the THREAD id. Offering only one made that gate
                # permanently unanswerable, which is #136 one surface over.
                # Mirrors the history double-write in `_persist_turn`.
                session_ids=_session_id_aliases(
                    mode=mode,
                    channel=str(event.get("channel", "")),
                    user_id=str(slack_user_id),
                    session_id=session_id,
                    is_threaded_reply=is_threaded_reply,
                    thread_replies=thread_replies,
                ),
            ):
                return

        # Response gate — only for thread continuations (mentions/DMs are
        # unconditional). Single-human threads bypass: every message in a
        # 1:1 thread is implicitly addressed to the bot.
        if mode == "thread_continuation" and thread_replies is not None:
            gate_history = _replies_to_gate_history(thread_replies, _bot_user_id)
            distinct_humans = _count_distinct_humans(
                thread_replies, slack_user_id, _bot_user_id
            )
            if gate_history and distinct_humans > 1:
                from openexecutive.integrations.response_gate import should_respond

                speaker_label = (
                    getattr(sender_person, "display_name", None)
                    or getattr(sender_person, "name", None)
                    or slack_user_id
                )
                decision = await should_respond(
                    user_text=cleaned,
                    author_display_name=speaker_label,
                    history=gate_history,
                    bot_display_name="Open Executive",
                    channel="slack",
                )
                if not decision.allow:
                    logger.info(
                        "Slack: response gate skipped message in session %s "
                        "(reason=%s)",
                        session_id,
                        decision.reason,
                    )
                    await asyncio.to_thread(
                        audit_log,
                        "integration_inbound",
                        f"Skipped: response gate (reason={decision.reason})",
                        actor="slack",
                        session_id=session_id,
                        details={
                            "channel": "slack",
                            "slack_user": slack_user_id,
                            "ts": event.get("ts"),
                            "thread_ts": thread_ts,
                            "outcome": "skipped_gate",
                            "skip_reason": decision.reason,
                        },
                    )
                    return

        # Fork the inbound message into the alerts triage pipeline. Runs in a
        # background thread/task so the reactive reply path is not delayed.
        try:
            from openexecutive.alerts.models import AlertEvent
            from openexecutive.alerts.pipeline import schedule_evaluation

            schedule_evaluation(
                AlertEvent(
                    source="slack",
                    external_id=str(event.get("ts") or thread_ts or ""),
                    channel=event.get("channel"),
                    user=slack_user_id,
                    body=cleaned,
                )
            )
        except Exception:
            logger.exception("Failed to schedule alert evaluation for Slack message")

        try:
            from openexecutive.integrations.channel_context import (
                attach_briefing_context,
                build_channel_context_block,
            )
            from openexecutive.knowledge.retriever import retrieve
            from openexecutive.memory.episodic import format_for_prompt
            from openexecutive.memory.session_store import load_messages
            from openexecutive.onboarding.profile_builder import load_or_create_profile
            from openexecutive.orchestrator.executive import Executive
            from openexecutive.orchestrator.mcp_gateway import get_active_gateway
            from openexecutive.orchestrator.session import Session

            # Everything from here to the persist below runs under the
            # conversation's lock: two messages in the same thread must not
            # both read history before either writes it.
            async with _session_lock(session_id):
                profile = await asyncio.to_thread(load_or_create_profile)
                # Where this conversation is happening, so a workflow that
                # raises an approval gate mid-turn can record that the
                # person's next reply HERE answers it (#136).
                session = Session(
                    session_id=session_id,
                    company_profile=profile if not profile.is_empty() else None,
                    origin_channel="slack",
                    origin_channel_ref=str(slack_user_id or ""),
                    caller_person_id=sender_person.id,
                )
                # Replay the stored conversation. Slack was the only chat
                # adapter with no history at all — every turn started cold,
                # so a multi-turn confirmation could never work (#136).
                history = await asyncio.to_thread(load_messages, session_id)
                if history:
                    session.conversation_history = history
                slack_user = event.get("user")
                if slack_user:
                    session.seen_channel_refs.add(("slack_dm", str(slack_user)))

                # Sender was already resolved at the roster gate above; reuse it
                # so we don't hit the DB twice in the hot path.
                person_id = sender_person.id

                # Multi-peer co-presence: enumerate other thread participants
                # from the already-fetched conversations_replies payload (one
                # API call total) and resolve each to a Person via
                # find_person_by_slack_id. Any client/API failure earlier left
                # thread_replies as None; that degrades to an empty list.
                co_present_person_ids: list[int] = []
                if thread_replies is not None:
                    seen: set[str] = set()
                    for msg in thread_replies:
                        uid = str(msg.get("user") or "")
                        if not uid or uid == str(slack_user) or uid in seen:
                            continue
                        if _bot_user_id and uid == _bot_user_id:
                            continue
                        seen.add(uid)
                        other = await asyncio.to_thread(find_person_by_slack_id, uid)
                        if other and other.id is not None:
                            co_present_person_ids.append(other.id)

                # Offloaded for the same reason api/routes/chat.py offloads them:
                # both are blocking (ChromaDB query + embedding, SQLite read) and
                # the handler now awaits on the application event loop, so running
                # them inline would stall every other request. `get_store()` hands
                # over the warm ChromaDB client the lifespan built, instead of
                # constructing a fresh PersistentClient on every message.
                from openexecutive.mcp_server.server import get_store

                retrieved_context, episodic_context = await asyncio.gather(
                    asyncio.to_thread(retrieve, query=cleaned, store=get_store()),
                    # session_id-scoped, matching discord_bot: an unscoped
                    # call mixes every other conversation's episodes into
                    # this turn.
                    asyncio.to_thread(format_for_prompt, session_id=session_id),
                )

                # The open-alert digest, so the Executive can actually answer
                # "what's on my plate?" from Slack and can cite a trustworthy
                # alert_id. Gated to the principal's DMs: the board is
                # company-wide, and pulling it into a shared channel would
                # leak every open item to everyone in that channel. Same gate
                # shape the outbound-context hydration below uses.
                briefing_context = await asyncio.to_thread(
                    attach_briefing_context,
                    session,
                    is_dm=(mode == "dm"),
                    person=sender_person,
                )

                # On the 1:1 DM path, hydrate with the context of any recent
                # outbound DM oe sent this user, so a reply oe solicited from
                # another session arrives with its backstory. One-shot consumed
                # inside the helper. Gated to DMs: a public-channel reply must not
                # pull private outbound context into a shared thread.
                chat_user_message = cleaned
                if mode == "dm" and slack_user_id:
                    from openexecutive.integrations.inbound_hydration import (
                        hydrate_user_message,
                    )

                    chat_user_message = await asyncio.to_thread(
                        hydrate_user_message,
                        channel="slack_dm",
                        channel_ref=str(slack_user_id),
                        user_message=cleaned,
                    )

                executive = Executive(mcp_gateway=get_active_gateway())
                response = await executive.chat(
                    user_message=chat_user_message,
                    session=session,
                    retrieved_context=retrieved_context,
                    episodic_context=episodic_context,
                    briefing_context=briefing_context,
                    channel_context_block=build_channel_context_block("slack"),
                    person_id=person_id,
                    co_present_person_ids=co_present_person_ids or None,
                )

                await say(text=response, thread_ts=thread_ts)

                # Persist AFTER the reply lands, and never let a persistence
                # failure trigger the user-facing error path — the user
                # already has their answer. Discord and Telegram both do the
                # same. Offloaded because save_message is sync SQLite and
                # this handler awaits on the application event loop.
                #
                # `cleaned`, NOT `chat_user_message`: the latter may carry the
                # one-shot <outbound_reply_context> block from
                # hydrate_user_message, and persisting that would replay a
                # consumed one-shot on every future turn.
                speaker = (
                    None
                    if mode == "dm"
                    else (
                        getattr(sender_person, "display_name", None)
                        or getattr(sender_person, "full_name", None)
                        or slack_user_id
                    )
                )
                # A mention that starts a thread gets its reply rooted at this
                # message's ts, so that thread's own session must also carry
                # the Q+A or its first continuation would start cold.
                also_session_id = None
                if mode != "dm" and not is_threaded_reply:
                    also_session_id = (
                        f"slack:thread:{event.get('channel', '')}:"
                        f"{event.get('ts', '')}"
                    )
                await asyncio.to_thread(
                    _persist_turn,
                    session_id=session_id,
                    title=_session_title(sender_person, mode),
                    created_at=session.created_at.isoformat(),
                    owner_person_id=person_id,
                    user_text=_format_user_content(cleaned, speaker),
                    assistant_text=response,
                    also_session_id=also_session_id,
                    sender_person_id=sender_person.id,
                )

        except Exception as exc:
            # Correlate the traceback with the audit trail. Without the
            # session_id the /audit page shows an integration_inbound row with
            # no matching turn and no way to find the log line that explains
            # it — which is how #136 stayed undiagnosable.
            logger.exception(
                "Slack: handler error session=%s user=%s channel=%s "
                "thread_ts=%s mode=%s",
                session_id,
                slack_user_id,
                event.get("channel"),
                thread_ts,
                mode,
            )
            # Reuse integration_inbound rather than minting a new event type:
            # the audit UI already styles and counts it, and `outcome` already
            # carries a vocabulary (rejected_unknown_sender, skipped_gate).
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    audit_log,
                    "integration_inbound",
                    f"Slack handler error for user={slack_user_id}: "
                    f"{type(exc).__name__}",
                    actor="slack",
                    session_id=session_id,
                    details={
                        "channel": "slack",
                        "slack_user": slack_user_id,
                        "slack_channel": event.get("channel"),
                        "ts": event.get("ts"),
                        "thread_ts": thread_ts,
                        "mode": mode,
                        "outcome": "handler_error",
                        "error": repr(exc)[:ERROR_DETAIL_LEN],
                    },
                )
            # Guard the apology itself. Telegram and Google Chat already do
            # this; Slack did not, so a failing say() escaped into Bolt.
            try:
                await say(
                    text=(
                        "I encountered an error processing your request. "
                        "Please try again."
                    ),
                    thread_ts=thread_ts,
                )
            except Exception:
                logger.exception(
                    "Slack: also failed to send the error reply for session=%s",
                    session_id,
                )

    @app.event("app_mention")
    async def handle_mention(event: dict, say, client) -> None:
        # Bolt auto-injects `client` (an AsyncWebClient) when listed in the
        # signature; we pass it through so the multi-peer thread-member
        # fetch can call conversations.replies without a separate import.
        #
        # Slack fires app_mention for an @-mention inside a DM too, and the
        # `message` listener has already taken that one as mode="dm". Handling
        # it twice meant two replies AND two different session ids, forking
        # the conversation's history — and a gate raised on the mention copy
        # recorded a session the user's next plain DM would never match.
        if event.get("channel_type") == "im":
            return
        async with inflight:
            await _handle_message(event, say, client=client, mode="mention")

    @app.event("message")
    async def handle_message(event: dict, say, client) -> None:
        # Slack delivers BOTH a generic `message` event AND `app_mention`
        # when the bot is mentioned in a channel/thread. Filter early so
        # only one handler fires.
        if event.get("bot_id") or event.get("subtype"):
            return  # bot messages, channel joins, edits, etc.

        channel_type = event.get("channel_type")
        if channel_type == "im":
            async with inflight:
                await _handle_message(event, say, client=client, mode="dm")
            return

        # If the bot is @-mentioned, `app_mention` will handle it. Skip
        # here to avoid double-firing. When _bot_user_id failed to resolve
        # at startup this filter is a no-op, but the bot-presence guard
        # inside _handle_message (see "Bot-presence guard for
        # thread_continuation mode") still catches the second invocation
        # because the bot has not yet engaged in any thread.
        text = event.get("text", "")
        if _bot_user_id and f"<@{_bot_user_id}>" in text:
            return

        # Auto-continuation only fires inside a thread the bot has
        # previously engaged in. The "has the bot replied here?" check
        # happens inside _handle_message where conversations_replies
        # is already being fetched; we only filter the cheap signals here.
        thread_ts = event.get("thread_ts")
        if not thread_ts or str(thread_ts) == str(event.get("ts") or ""):
            return  # not a threaded reply (thread starters go through app_mention)

        async with inflight:
            await _handle_message(
                event, say, client=client, mode="thread_continuation"
            )

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    return app, handler


async def _run_slack_bot_async() -> None:
    _, handler = await create_slack_app()
    logger.info("Starting Slack bot in socket mode...")
    # start_async() == connect_async() + sleep(inf): correct for a standalone
    # process, but never call it from the FastAPI lifespan — it would never
    # return. The lifespan uses connect_async()/close_async() instead.
    await handler.start_async()


def run_slack_bot() -> None:
    """Standalone entrypoint: ``python -m openexecutive.integrations.slack_bot``.

    Still supported for running the bot in isolation. Normal operation now
    starts the listener from the FastAPI lifespan instead (see api/main.py),
    so ``make dev`` brings Slack up with the rest of the app.
    """
    asyncio.run(_run_slack_bot_async())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_slack_bot()
