"""Discord bot integration for Open Executive.

Mirrors the structure of slack_bot.py. In production the bot is embedded in
the FastAPI lifespan (api/main.py) so it shares the same /data volume as the
API. `run_discord_bot()` / `python -m openexecutive.integrations.discord_bot`
is a standalone dev entry point — useful for iterating on bot-only changes
without restarting the API. Both rely on `create_discord_bot()` returning a
`commands.Bot` whose `.start()` / `.close()` work in any event loop.

Supported interactions:
- DM the bot → Executive replies in DM
- @mention in a guild channel → Executive replies in a thread
- /ask <prompt> slash command → deferred Executive reply
- /today slash command → Today dashboard summary
- post_notification() helper for outbound alerts/digests
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class MentionRouteResult:
    """Return value of a lazy ``response_router`` passed into ``_handle_message``.

    The router inspects the generated reply text (after ``executive.chat``
    returns and before chunked sending) and decides where the reply
    should land. For the @mention auto-thread router specifically:
    - Short reply → ``send_fn`` posts inline via ``message.reply``;
      ``promoted_*`` fields stay None.
    - Long reply → router creates a brand-new bot-owned thread, returns
      a ``send_fn`` that posts into the thread, and surfaces the new
      thread's id / title / object so ``_handle_message`` can
      (a) double-write the turn into the thread's session for future
      thread-continuation context, and (b) schedule the auto-title
      rename even when the rolling channel session has prior history.
    """

    send_fn: Callable[[str], Awaitable[None]]
    promoted_thread_id: str | None = None
    promoted_session_title: str | None = None
    promoted_thread: object | None = field(default=None, repr=False)

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?\d+>")
_DISCORD_MAX_LEN = 2000

# Discord caps thread names at 100 chars. We aim shorter (80) so a model that
# slightly overshoots the requested length still fits without truncation that
# could chop a word mid-character. Title generation runs through Haiku with a
# small token budget so this is a safety net, not the primary length control.
_THREAD_TITLE_MAX_LEN = 80
_THREAD_TITLE_PLACEHOLDER = "New chat"

# Strong refs to background rename tasks. Without this, the asyncio GC can
# cancel a not-yet-awaited task before it runs. Mirrors the pattern in
# memory/episodic.py for the extraction background task.
_thread_rename_tasks: set[asyncio.Task[None]] = set()

# Per-session lock so concurrent messages in the same DM/thread serialize.
# Without this, two handlers in the same session both load the same history,
# both produce responses ignoring each other's reply, and both append to the
# DB — leading to interleaved turns and confused subsequent responses.
# Mirrors the per-chat lock in telegram_bot.py.
_session_locks: dict[str, asyncio.Lock] = {}


def _session_lock(session_id: str) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def _clean_message(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def _session_title_for_message(message: object, mode: str) -> str:
    """Human-readable title shown in the session list. Falls back gracefully
    when the message stub doesn't have the attribute (mocked tests, slash
    interactions)."""
    author = getattr(message, "author", None)
    name = getattr(author, "display_name", None) or getattr(author, "name", None) or "user"
    if mode == CLASSIFY_DM:
        return f"Discord DM ({name})"
    channel = getattr(message, "channel", None)
    channel_name = getattr(channel, "name", None)
    if mode == CLASSIFY_MENTION_CHANNEL and channel_name:
        # Rolling channel sessions are per-user — include the speaker so
        # the operator's Recent-chats sidebar can tell Alice's #general
        # session apart from Bob's.
        return f"Discord #{channel_name} ({name})"
    if channel_name:
        return f"Discord #{channel_name}"
    return "Discord thread"


def _compute_session_id(
    mode: str,
    discord_user_id: str,
    channel_id: str,
    new_thread_id: str | None = None,
    fallback_message_id: str | None = None,
) -> str:
    """Deterministic session id keyed to the conversation surface.

    - DM → 'discord:dm:{user_id}' — one long-lived conversation per user.
    - Thread continuation / mention inside an existing thread →
      'discord:thread:{channel_id}' (the channel id IS the thread id).
    - Mention in a plain text channel (lazy auto-thread router) →
      'discord:channel:{channel_id}:{user_id}' — rolling per-(channel,
      user) session so the bot remembers prior in-channel @mentions from
      the same person, analogous to how DMs roll forever per user. When
      the router promotes a reply to a new thread, that thread also
      gets its own 'discord:thread:{new_thread_id}' session via a
      double-write at persistence time.
    - Legacy mention path (CLASSIFY_MENTION, in an existing human
      thread) → 'discord:thread:{channel_id}' — preserved bit-for-bit.
    - Fallback → 'discord:msg:{message_id}'.
    """
    if mode == CLASSIFY_DM:
        return f"discord:dm:{discord_user_id}"
    if mode == CLASSIFY_THREAD_CONTINUATION:
        return f"discord:thread:{channel_id}"
    if mode == CLASSIFY_MENTION_CHANNEL:
        return f"discord:channel:{channel_id}:{discord_user_id}"
    if mode == CLASSIFY_MENTION:
        if new_thread_id is not None:
            return f"discord:thread:{new_thread_id}"
        if fallback_message_id is not None:
            return f"discord:msg:{fallback_message_id}"
        return f"discord:thread:{channel_id}"
    return f"discord:user:{discord_user_id}"


# Routing modes returned by _classify_inbound() — strings, not an enum, to
# keep the module importable in unit tests without instantiating discord.py.
CLASSIFY_SKIP = "skip"
CLASSIFY_DM = "dm"
CLASSIFY_MENTION = "mention"  # @mention inside an existing (non-bot-owned) thread
CLASSIFY_MENTION_CHANNEL = "mention_channel"  # @mention in a plain text channel
CLASSIFY_THREAD_CONTINUATION = "thread_continuation"


def _classify_inbound(message: object, bot_user: object) -> str:
    """Decide whether and how to respond to an inbound Discord message.

    Returns one of CLASSIFY_*. Logic:
    - Author is another bot → skip (avoids bot-loop / wasted LLM tokens).
    - DM channel → always respond ('dm').
    - Thread the bot itself created, not archived → respond inline
      ('thread_continuation'). Bot-owned check (vs "any thread the bot is
      in") avoids hijacking threads humans add the bot to. Archived check
      avoids auto-resurrecting threads users intentionally closed.
    - Explicit @mention inside an existing (human-owned) thread →
      'mention' (reply inline; Discord rejects nested threads).
    - Explicit @mention in a plain TextChannel → 'mention_channel'
      (lazy router decides inline vs new thread based on reply length).
    - Otherwise → skip ('skip').
    """
    import discord

    author = getattr(message, "author", None)
    if author is not None and getattr(author, "bot", False):
        return CLASSIFY_SKIP

    channel = getattr(message, "channel", None)
    if isinstance(channel, discord.DMChannel):
        return CLASSIFY_DM
    if (
        isinstance(channel, discord.Thread)
        and bot_user is not None
        and getattr(channel, "owner_id", None) == getattr(bot_user, "id", None)
        and not getattr(channel, "archived", False)
    ):
        return CLASSIFY_THREAD_CONTINUATION
    if bot_user is not None and bot_user in getattr(message, "mentions", []):
        # Mentions inside any thread → keep the existing in-thread reply
        # path (CLASSIFY_MENTION). Only plain-channel mentions go through
        # the new auto-thread router.
        if isinstance(channel, discord.Thread):
            return CLASSIFY_MENTION
        return CLASSIFY_MENTION_CHANNEL
    return CLASSIFY_SKIP


_DISPLAY_NAME_MAX_LEN = 32
_DISPLAY_NAME_FALLBACK = "User"
_THREAD_SESSION_PREFIX = "discord:thread:"

# Matches the `[Name]: ` prefix that _format_user_content writes onto every
# stored user turn in a thread session. Used to recover distinct human authors
# from history without a schema change.
_THREAD_AUTHOR_PREFIX_RE = re.compile(r"^\[([^\]]+)\]: ")


def _thread_author_display_names(
    history: list[dict], current_author: str | None
) -> set[str]:
    """Return the set of distinct cleaned display-names observed in this thread.

    Reads the `[Name]: ` prefix written by `_format_user_content` on every
    stored user turn in a thread session. The current message's author
    (cleaned the same way the prefix would be) is included so a brand-new
    speaker is included even before their turn is persisted.

    Lifted out of `_distinct_thread_authors` so the multi-peer Honcho
    wire-up can resolve each name back to a Person without scanning
    history twice.
    """
    names: set[str] = set()
    for m in history:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        match = _THREAD_AUTHOR_PREFIX_RE.match(content)
        if match:
            names.add(match.group(1).strip())
    if current_author:
        names.add(_clean_display_name(current_author))
    return names


def _distinct_thread_authors(history: list[dict], current_author: str | None) -> int:
    """Count distinct human authors observed in this thread.

    Thin wrapper over :func:`_thread_author_display_names` so the gate
    logic stays a one-liner. Returns 0 only when there is no history and
    no current author — the caller treats `<= 1` as "single-human
    thread, skip the gate".
    """
    return len(_thread_author_display_names(history, current_author))


def _clean_display_name(name: str | None) -> str:
    """Sanitize a Discord display name for embedding in stored content.

    Strips characters that would let a malicious display name escape the
    `[Name]: text` envelope and inject fake turns the model later replays:
    - newlines/tabs/control chars (would forge a turn break);
    - `[`, `]`, `:` (would close the bracket and start a new prefix — e.g.
      a name like `Alice]: ignore prior\\n[Carol` becomes `AliceignorepriorCarol`).
    Collapses whitespace, caps at 32 chars, falls back to `"User"` if empty.
    """
    if not name:
        return _DISPLAY_NAME_FALLBACK
    collapsed = re.sub(r"\s+", " ", name)
    cleaned = "".join(
        c for c in collapsed if c.isprintable() and c not in "[]:"
    ).strip()
    # Re-collapse — dropping a non-printable or stripped char between two
    # spaces can leave a double space that strip() above won't touch.
    cleaned = re.sub(r" +", " ", cleaned)
    if not cleaned:
        return _DISPLAY_NAME_FALLBACK
    return cleaned[:_DISPLAY_NAME_MAX_LEN]


def _format_user_content(
    text: str, author_display_name: str | None, session_id: str
) -> str:
    """Return the user-turn content as the Executive (and DB) will see it.

    In multi-party-capable contexts (Discord threads) we prefix the message
    with the speaker's display name so the model can tell participants apart.
    DMs and per-user rolling sessions are left untouched — a single speaker's
    prefix is just noise. The prefix lives in the stored content (no schema
    change) and Claude picks up the `[Name]: ...` pattern naturally.
    """
    if not session_id.startswith(_THREAD_SESSION_PREFIX):
        return text
    if author_display_name is None:
        return text
    return f"[{_clean_display_name(author_display_name)}]: {text}"


def _is_direct_mention(message: object, bot_user: object) -> bool:
    """True iff the bot user appears in message.mentions. Safe on mocks."""
    if bot_user is None:
        return False
    return bot_user in getattr(message, "mentions", [])


def _should_promote_to_thread(response: str, threshold_chars: int) -> bool:
    """Decide whether an @mention reply is long enough to warrant a thread.

    Single signal: total reply length crosses the configured threshold
    (default 1500 chars). Only genuinely long, channel-cluttering essays
    get promoted to a thread; everything shorter stays inline so simple
    requests are answered right there in the channel.

    An earlier version also promoted any reply with 3+ paragraphs, but the
    Executive routinely formats even short answers as a few short
    paragraphs or bullet groups, so that trigger fired on nearly every
    reply and buried quick answers in threads. Length alone keeps the
    decision predictable and well-understood — no hidden LLM-based
    "is this interesting" call.
    """
    return len(response) >= threshold_chars


# Canonical bare greetings used by ``_is_simple_greeting``. Members are
# the *normalized* form: lowercased, internal whitespace collapsed, trailing
# punctuation stripped. We exclude conversational fillers like "thanks",
# "ok", "cool", "lol" — those usually appear mid-conversation and the user
# typically still wants a thread for the reply.
_SIMPLE_GREETINGS: frozenset[str] = frozenset({
    "hi", "hey", "hello", "yo", "sup", "gm", "gn",
    "howdy", "hola", "wassup", "whatup", "whats up", "what's up",
    "morning", "afternoon", "evening",
    "good morning", "good afternoon", "good evening", "good night",
    "hi there", "hey there", "hello there",
})

# Trailing addressee terms that don't change the greeting-ness of a message.
# "hey rufus" / "hi team" / "hello everyone" still count as greetings.
_GREETING_ADDRESSEES: frozenset[str] = frozenset({
    "there", "exec", "executive", "oe", "open executive",
    "rufus", "all", "team", "everyone", "yall", "y'all", "folks",
})


def _is_simple_greeting(text: str) -> bool:
    """True iff ``text`` is a bare casual hello with no substantive content.

    Used by the ``thread_unless_greeting`` reply mode so that "hi" or
    "good morning rufus" stays inline in the channel, while everything
    else (questions, requests, tasks) gets promoted to a fresh thread.

    Detection is intentionally set-based and case-insensitive, not LLM-
    based: a missed edge case is one frozenset entry away, the decision
    is cheap, and a unit test can pin the behavior precisely. The bot
    mention itself is already stripped by ``_clean_message`` upstream,
    so ``text`` here is the typed user message body (NOT any attachment-
    extracted document text — see ``user_text_for_routing`` snapshot in
    ``on_message``).
    """
    if not text:
        # Empty content (e.g. an attachment-only @mention) is not a
        # "greeting" — let the router decide based on its other signals.
        return False
    # Normalize: lowercase, collapse internal whitespace. We deliberately
    # do NOT strip non-trailing punctuation — "hi, can you help me?" must
    # NOT collapse to just "hi" (it's a real request with a friendly
    # opener). But trailing punctuation, symbols (emoji), and whitespace
    # should not block recognition: "hi 👋", "hi…", and "hey rufus :)"
    # are all the user being friendly, not adding substance. Unicode
    # category check handles ellipsis, smart quotes, and emoji without
    # hand-curating a strip set.
    normalized = re.sub(r"\s+", " ", text.strip()).lower()
    while normalized and (
        normalized[-1].isspace()
        or unicodedata.category(normalized[-1])[0] in {"P", "S"}
    ):
        normalized = normalized[:-1]
    if not normalized:
        return False
    if normalized in _SIMPLE_GREETINGS:
        return True
    # Try peeling a trailing addressee: "hey rufus" → "hey". Also strip
    # a trailing comma from the prefix so "hi, rufus" → "hi" matches
    # symmetrically with "hi rufus," (which was already stripped above).
    for suffix in _GREETING_ADDRESSEES:
        marker = " " + suffix
        if normalized.endswith(marker):
            prefix = normalized[: -len(marker)].rstrip().rstrip(",")
            if prefix in _SIMPLE_GREETINGS:
                return True
    return False


def _resolve_thread_promotion(
    reply_mode: str,
    user_text: str,
    response_text: str,
    threshold_chars: int,
) -> bool:
    """Decide whether a plain-channel @mention reply should open a thread.

    Single source of truth for the four-way mode switch, extracted from
    the ``on_message`` closure so it can be unit-tested without spinning
    up a full ``commands.Bot``. The closure passes the user's incoming
    text (already mention-stripped by ``_clean_message``) and the
    generated response; this function decides ``promote`` based on the
    active mode:

    - ``always_inline``           — never promote.
    - ``always_thread``           — always promote.
    - ``thread_unless_greeting``  — promote unless the user message is a
      bare hello (see :func:`_is_simple_greeting`).
    - ``auto`` (or any other)     — length-only heuristic on the
      generated response (legacy).
    """
    if reply_mode == "always_inline":
        return False
    if reply_mode == "always_thread":
        return True
    if reply_mode == "thread_unless_greeting":
        return not _is_simple_greeting(user_text)
    return _should_promote_to_thread(response_text, threshold_chars)


async def _should_respond_in_thread(
    user_text: str,
    author_display_name: str | None,
    history: list[dict],
    bot_display_name: str | None,
) -> bool:
    """Discord-specific bool wrapper around the shared response gate.

    Production code calls ``response_gate.should_respond`` directly to
    get the :class:`GateDecision` (allow + reason + raw) needed for the
    audit row. This wrapper exists for the direct-call tests in
    ``test_discord_bot.py`` that exercise the gate's YES/NO behavior
    without needing the audit-emit path — they pass the provider mock
    through here and just want a bool back.
    """
    from openexecutive.integrations.response_gate import should_respond

    decision = await should_respond(
        user_text=user_text,
        author_display_name=author_display_name,
        history=history,
        bot_display_name=bot_display_name,
        channel="discord",
    )
    return decision.allow


def _split_message(text: str, limit: int = _DISCORD_MAX_LEN) -> list[str]:
    """Split a long response into chunks that fit Discord's per-message limit.

    Splits on whitespace where possible to avoid breaking words.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    return parts


async def _generate_thread_title(user_msg: str, assistant_msg: str) -> str | None:
    """Generate a short, topic-specific thread title via Haiku.

    Thin Discord-specific wrapper around the shared title helper — applies
    the Discord 80-char thread-name cap. Returns ``None`` on any failure
    (caller keeps the placeholder thread name).
    """
    from openexecutive.utils.session_title import generate_session_title

    return await generate_session_title(
        user_msg, assistant_msg, max_len=_THREAD_TITLE_MAX_LEN
    )


def _schedule_thread_rename(
    thread: object,
    session_id: str,
    user_msg: str,
    assistant_msg: str,
) -> None:
    """Fire-and-forget thread rename. Never raises into the caller.

    On a brand-new bot-owned thread, after the first assistant reply has been
    sent and persisted, generate a topic-specific title via Haiku, then update
    both Discord (thread.edit) and the local session row (so the web UI
    session list shows the same good title).
    """
    from openexecutive.memory.session_store import update_session_title

    async def _run() -> None:
        try:
            title = await _generate_thread_title(user_msg, assistant_msg)
            if not title:
                return
            try:
                await thread.edit(name=title)  # type: ignore[attr-defined]
            except Exception:
                logger.exception(
                    "Discord: thread.edit(name=...) failed for session %s", session_id
                )
                # Still update the local session title — useful for the web UI
                # even if the Discord rename failed (rate-limited, perms, etc).
            try:
                update_session_title(session_id, title)
            except Exception:
                logger.exception(
                    "Discord: update_session_title failed for session %s", session_id
                )
        except Exception:
            logger.exception("Discord: thread rename task crashed")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — should not happen from the on_message handler,
        # but bail silently rather than spinning up a thread for a cosmetic
        # operation.
        return
    task = loop.create_task(_run())
    _thread_rename_tasks.add(task)
    task.add_done_callback(_thread_rename_tasks.discard)


def _is_rostered(discord_user_id: str) -> bool:
    """Whether this Discord user maps to a Person on the roster.

    The same lookup ``_handle_message``'s roster gate makes, exposed so
    ``on_message`` can skip work it would otherwise do *before* that gate
    runs. Kept as a boolean rather than returning the Person: the caller
    only needs the decision, and the gate below still does its own lookup
    so its audit rows keep their existing shape.
    """
    if not discord_user_id:
        return False
    from openexecutive.people.store import find_person_by_discord_id

    try:
        return find_person_by_discord_id(discord_user_id) is not None
    except Exception:
        # A roster lookup that errors must not be read as "rostered".
        logger.exception("Discord: roster lookup failed for user=%s", discord_user_id)
        return False


async def _handle_message(
    text: str,
    discord_user_id: str,
    discord_channel: str,
    message_id: str,
    thread_id: str | None,
    send_fn,  # async callable(str) -> None
    is_dm: bool,
    session_id: str,
    session_title: str,
    on_first_turn_complete: Callable[[str, str], Awaitable[None]] | None = None,
    author_display_name: str | None = None,
    gate_eligible: bool = False,
    bot_display_name: str | None = None,
    attachment_blocks: list[dict] | None = None,
    attachment_count: int = 0,
    co_present_discord_user_ids: list[str] | None = None,
    response_router: Callable[[str], Awaitable[MentionRouteResult]] | None = None,
) -> None:
    """Core handler shared by on_message and slash commands.

    `on_first_turn_complete(user_msg, assistant_msg)` runs after a successful
    reply has been sent and persisted, but ONLY when there was no prior
    history for the session (i.e. this was the first turn). Callers use it to
    trigger thread renames; pass None to opt out.

    `attachment_blocks` is a list of Anthropic image content blocks assembled
    by process_attachments(). Text-extracted document content is expected to
    already be prepended to `text` by the caller.

    `attachment_count` is how many files the message carried, recorded in the
    audit row. It is passed separately from `attachment_blocks` because the
    caller skips attachment processing for an unrostered sender — that is
    exactly the case where /audit should still show a file was sent.

    `response_router`, if provided, is awaited with the full reply text
    AFTER ``executive.chat`` returns and BEFORE the chunked send loop.
    The returned :class:`MentionRouteResult` selects the actual ``send_fn``
    (so the route decision sees the full reply, not just the first chunk)
    and optionally surfaces a brand-new bot-owned thread whose session
    should also receive the turn (double-write) so future thread-
    continuation messages have the original Q+A in context. Used by the
    plain-channel @mention auto-thread path.
    """
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "integration_inbound",
        f"Inbound discord from user={discord_user_id} channel={discord_channel}: {text[:160]}",
        actor="discord",
        session_id=session_id,
        details={
            "channel": "discord",
            "discord_channel": discord_channel,
            "discord_user": discord_user_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "text_len": len(text),
            "attachments": attachment_count,
        },
    )

    # Roster gate. Drop messages from any Discord user not represented
    # in the People roster — there's no other access control. Emit a
    # second audit row tagged ``rejected_unknown_sender`` so /audit
    # shows both the attempt and the rejection.
    from openexecutive.people.store import find_person_by_discord_id
    sender_person = (
        find_person_by_discord_id(discord_user_id) if discord_user_id else None
    )
    if sender_person is None:
        audit_log(
            "integration_inbound",
            f"Rejected: discord user={discord_user_id} not in People roster",
            actor="discord",
            session_id=session_id,
            details={
                "channel": "discord",
                "discord_user": discord_user_id,
                "outcome": "rejected_unknown_sender",
            },
        )
        return

    # WaitForHuman inbound resolver — check before alert triage.
    if discord_user_id:
        try:
            from openexecutive.people.store import find_person_by_discord_id
            from openexecutive.workflows.inbound_resolver import resolve_inbound_message
            from openexecutive.workflows.resumer import apply_resolution

            person = find_person_by_discord_id(discord_user_id)
            if person is not None and person.id is not None:
                resolution = await resolve_inbound_message(
                    channel="discord",
                    channel_ref=discord_user_id,
                    from_person_id=person.id,
                    text=text,
                    message_id=message_id,
                    in_reply_to=thread_id or "",
                )
                if resolution is not None and resolution.run_id:
                    success = await apply_resolution(resolution.run_id, resolution)
                    if success:
                        await send_fn("Got it — your response has been recorded.")
                        return
        except Exception:
            logger.exception("Discord: inbound resolver check failed")

    # Fork into alerts triage pipeline.
    try:
        from openexecutive.alerts.models import AlertEvent
        from openexecutive.alerts.pipeline import schedule_evaluation

        schedule_evaluation(
            AlertEvent(
                source="discord",
                external_id=message_id,
                channel=discord_channel,
                user=discord_user_id,
                body=text,
            )
        )
    except Exception:
        logger.exception("Failed to schedule alert evaluation for Discord message")

    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.memory.session_store import (
        create_session,
        load_messages,
        save_message,
        update_session_timestamp,
    )
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.session import Session

    # Response gate: in a bot-owned thread with prior turns, ask the shared
    # gate (response_gate.should_respond) whether this message warrants a
    # reply. Skipped on DMs, explicit @mentions, and the first turn of a
    # brand-new thread (caller signals via gate_eligible). Loads history
    # *outside* the session lock so a slow gate call doesn't serialize
    # unrelated incoming messages; the locked block re-loads fresh.
    if gate_eligible:
        from openexecutive.config import get_settings
        from openexecutive.integrations.response_gate import should_respond

        if get_settings().discord_thread_response_gate_enabled:
            gate_history = load_messages(session_id)
            # Single-human thread bypass: the gate exists to suppress replies
            # to side chatter between humans. If only one human has ever
            # spoken in this thread, every message is implicitly addressed to
            # the bot — running the gate just risks false negatives.
            single_human = _distinct_thread_authors(gate_history, author_display_name) <= 1
            if gate_history and not single_human:  # first-turn passes through
                decision = await should_respond(
                    user_text=text,
                    author_display_name=author_display_name,
                    history=gate_history,
                    bot_display_name=bot_display_name,
                    channel="discord",
                )
                if not decision.allow:
                    logger.info(
                        "Discord: response gate skipped message in session %s "
                        "(reason=%s)",
                        session_id,
                        decision.reason,
                    )
                    audit_log(
                        "integration_inbound",
                        f"Skipped: response gate (reason={decision.reason})",
                        actor="discord",
                        session_id=session_id,
                        details={
                            "channel": "discord",
                            "discord_user": discord_user_id,
                            "message_id": message_id,
                            "thread_id": thread_id,
                            "outcome": "skipped_gate",
                            "skip_reason": decision.reason,
                        },
                    )
                    return

    # Author-prefixed content for the Executive and the persisted user turn.
    # In threads this becomes `[Alice]: ...`; in DMs it's identical to `text`.
    formatted_user_text = _format_user_content(text, author_display_name, session_id)

    response: str | None = None
    is_first_turn = False
    # ``route_result`` is referenced after the locked block for the
    # post-promotion rename — initialize at function scope so any future
    # early-return inside the try doesn't leave the post-lock reference
    # reading a stale name from an earlier turn.
    route_result: MentionRouteResult | None = None
    async with _session_lock(session_id):
        try:
            profile = load_or_create_profile()
            session = Session(
                session_id=session_id,
                company_profile=profile if not profile.is_empty() else None,
            )
            history = load_messages(session_id)
            if history:
                session.conversation_history = history
            else:
                is_first_turn = True
            if is_dm and discord_user_id:
                session.seen_channel_refs.add(("discord_dm", discord_user_id))

            retrieved_context = retrieve(query=text)
            episodic_context = format_for_prompt(session_id=session_id)

            # Sender was already resolved at the roster gate above; reuse it
            # so we don't hit the DB twice in the hot path.
            person_id = sender_person.id

            # Multi-peer co-presence: resolve every other thread
            # participant's Person.id so Honcho can add them as peers
            # in the session (enables peer-of-peer reasoning via the
            # ask_about_person tool). The caller passed us the raw
            # discord-user-id list because they have `message` in
            # scope; we do the Person-lookup here so the resolution
            # logic stays colocated with `find_person_by_discord_id`.
            co_present_person_ids: list[int] = []
            for other_uid in co_present_discord_user_ids or ():
                if other_uid == discord_user_id:
                    continue  # sender already covered by person_id
                other = find_person_by_discord_id(other_uid)
                if other and other.id is not None:
                    co_present_person_ids.append(other.id)

            # On the 1:1 DM path, hydrate the turn with the context of any
            # recent outbound DM oe sent this person (e.g. the principal told
            # oe to "DM Alex about X" in another session). Inject into the
            # copy handed to the model only — NOT formatted_user_text, which is
            # persisted to history below and must stay free of this one-shot
            # block. One-shot consumed inside the helper.
            chat_user_message = formatted_user_text
            if is_dm and discord_user_id:
                from openexecutive.integrations.inbound_hydration import (
                    hydrate_user_message,
                )

                chat_user_message = hydrate_user_message(
                    channel="discord_dm",
                    channel_ref=discord_user_id,
                    user_message=formatted_user_text,
                )

            executive = Executive(mcp_gateway=get_active_gateway())
            response = await executive.chat(
                user_message=chat_user_message,
                session=session,
                retrieved_context=retrieved_context,
                episodic_context=episodic_context,
                attachment_blocks=attachment_blocks or None,
                person_id=person_id,
                co_present_person_ids=co_present_person_ids or None,
            )

            # Late routing: for the @mention auto-thread path, the router
            # decides inline-vs-thread after seeing the full reply. The
            # decision MUST happen before the chunked send loop because
            # otherwise a long reply could land its first chunk in the
            # channel before we realize it should have gone to a thread.
            effective_send_fn = send_fn
            if response_router is not None:
                try:
                    route_result = await response_router(response)
                    effective_send_fn = route_result.send_fn
                except Exception:
                    logger.exception(
                        "Discord: response_router failed for session %s — "
                        "falling back to the default send_fn",
                        session_id,
                    )

            # Empty-reply guard. ``_split_message("")`` yields ``[""]`` and
            # Discord rejects empty message sends with HTTP 400. An empty
            # response usually means executive.chat short-circuited (e.g.
            # tool-use loop bailout) and there's nothing to say — better
            # to silently no-op than to spam the channel with an error
            # message that itself confuses users.
            if response.strip():
                for chunk in _split_message(response):
                    await effective_send_fn(chunk)
            else:
                logger.info(
                    "Discord: executive returned empty response for session %s — "
                    "skipping send",
                    session_id,
                )

        except Exception as e:
            logger.error("Discord handler error: %s", e, exc_info=True)
            await send_fn(
                "I encountered an error processing your request. Please try again."
            )
            return

        # Persist only after a successful reply. Failures here must not trigger
        # a user-facing error — the user already got their answer.
        #
        # Channel sessions are owned by the resolved sender if mapped to a
        # Person, otherwise fall back to the principal so unrostered channel
        # threads still appear in the operator's sidebar (instead of becoming
        # invisible NULL-owner rows).
        from openexecutive.people.store import find_principal_person
        session_owner_id = person_id
        if session_owner_id is None:
            principal = find_principal_person()
            session_owner_id = principal.id if principal is not None else None
        try:
            create_session(
                session_id,
                session_title,
                session.created_at.isoformat(),
                caller_person_id=session_owner_id,
            )
            save_message(session_id, "user", formatted_user_text)
            save_message(session_id, "assistant", response)
            update_session_timestamp(session_id)
        except Exception:
            logger.exception(
                "Discord: failed to persist turn for session %s", session_id
            )

        # Promoted-thread double-write: when the router opened a brand-
        # new bot-owned thread for this reply, also persist the turn
        # under that thread's session_id so future thread-continuation
        # messages in it see the original Q+A in context. The rolling
        # channel session above is the primary source of truth; this is
        # an idempotent secondary copy.
        if route_result is not None and route_result.promoted_thread_id:
            promoted_session_id = f"discord:thread:{route_result.promoted_thread_id}"
            promoted_title = (
                route_result.promoted_session_title or session_title
            )
            try:
                create_session(
                    promoted_session_id,
                    promoted_title,
                    session.created_at.isoformat(),
                    caller_person_id=session_owner_id,
                )
                save_message(promoted_session_id, "user", formatted_user_text)
                save_message(promoted_session_id, "assistant", response)
                update_session_timestamp(promoted_session_id)
            except Exception:
                logger.exception(
                    "Discord: failed to double-write promoted thread session %s",
                    promoted_session_id,
                )

    # First-turn hook (e.g. rename a freshly-created thread). Runs outside the
    # session lock so a slow Haiku call doesn't block the next inbound turn
    # in the same conversation. Hook is fire-and-forget — exceptions inside
    # must not bubble up to the on_message handler.
    if is_first_turn and response is not None and on_first_turn_complete is not None:
        try:
            await on_first_turn_complete(text, response)
        except Exception:
            logger.exception(
                "Discord: on_first_turn_complete hook failed for session %s", session_id
            )

    # Promoted-thread auto-rename: the thread is brand-new (fresh
    # placeholder name) regardless of whether the rolling channel
    # session had prior turns, so the rename hook fires every time the
    # router promotes — not gated on is_first_turn.
    if (
        route_result is not None
        and route_result.promoted_thread is not None
        and route_result.promoted_thread_id is not None
        and response is not None
    ):
        try:
            _schedule_thread_rename(
                route_result.promoted_thread,
                f"discord:thread:{route_result.promoted_thread_id}",
                text,
                response,
            )
        except Exception:
            logger.exception(
                "Discord: failed to schedule rename for promoted thread"
            )


def create_discord_bot():
    import discord
    from discord import app_commands
    from discord.ext import commands

    from openexecutive.config import get_settings

    settings = get_settings()

    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN must be set to run the Discord bot")

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guild_messages = True
    intents.dm_messages = True

    # Access control is roster-driven now: only Discord users with a
    # matching `discord_user_id` on a non-archived Person row can talk
    # to the bot. The old DISCORD_ALLOWED_USER_IDS env var has been
    # removed — manage access via the /people UI.

    bot = commands.Bot(command_prefix="!", intents=intents)

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #

    @bot.tree.command(name="ask", description="Ask the Executive anything")
    @app_commands.describe(prompt="Your question or request")
    async def slash_ask(interaction: discord.Interaction, prompt: str) -> None:
        # Roster gate BEFORE defer. If we deferred first and then no
        # follow-up was sent, Discord would hang on a "thinking…"
        # spinner until the 15-minute interaction-token expiry.
        from openexecutive.people.store import find_person_by_discord_id
        if find_person_by_discord_id(str(interaction.user.id)) is None:
            from openexecutive.audit import log_event as audit_log
            audit_log(
                "integration_inbound",
                f"Rejected /ask: discord user={interaction.user.id} not in People roster",
                actor="discord",
                details={
                    "channel": "discord",
                    "discord_user": str(interaction.user.id),
                    "command": "ask",
                    "outcome": "rejected_unknown_sender",
                },
            )
            await interaction.response.send_message(
                "You don't appear in the People roster — ask the admin to add you.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        async def send_fn(text: str) -> None:
            await interaction.followup.send(text)

        cleaned = prompt.strip()
        if not cleaned:
            await interaction.followup.send("Please provide a non-empty prompt.")
            return

        is_dm = isinstance(interaction.channel, discord.DMChannel)
        user_id = str(interaction.user.id)
        # /ask continuity rules:
        # - DM: join the same conversation as regular DMs (discord:dm:{user_id}).
        # - Inside a bot-owned thread: join that thread's conversation so /ask
        #   sees the prior context (avoids a confusing fresh start).
        # - Anywhere else: per-user rolling session — slash commands are
        #   user-private and there's no shared thread to scope to.
        if is_dm:
            ask_session_id = f"discord:dm:{user_id}"
            ask_title = f"Discord DM ({interaction.user.display_name})"
        elif (
            isinstance(interaction.channel, discord.Thread)
            and bot.user is not None
            and interaction.channel.owner_id == bot.user.id
        ):
            ask_session_id = f"discord:thread:{interaction.channel.id}"
            ask_title = f"Discord #{interaction.channel.name}"
        else:
            ask_session_id = f"discord:user:{user_id}"
            ask_title = f"Discord /ask ({interaction.user.display_name})"

        await _handle_message(
            text=cleaned,
            discord_user_id=user_id,
            discord_channel=str(interaction.channel_id),
            message_id=str(interaction.id),
            thread_id=None,
            send_fn=send_fn,
            is_dm=is_dm,
            session_id=ask_session_id,
            session_title=ask_title,
            author_display_name=getattr(interaction.user, "display_name", None),
        )

    @bot.tree.command(name="today", description="Get the Today dashboard summary")
    async def slash_today(interaction: discord.Interaction) -> None:
        # Roster gate BEFORE defer so a rejected user gets an immediate
        # ephemeral message instead of a hung "thinking…" spinner.
        from openexecutive.people.store import find_person_by_discord_id
        if find_person_by_discord_id(str(interaction.user.id)) is None:
            from openexecutive.audit import log_event as audit_log
            audit_log(
                "integration_inbound",
                f"Rejected /today: discord user={interaction.user.id} not in People roster",
                actor="discord",
                details={
                    "channel": "discord",
                    "discord_user": str(interaction.user.id),
                    "command": "today",
                    "outcome": "rejected_unknown_sender",
                },
            )
            await interaction.response.send_message(
                "You don't appear in the People roster — ask the admin to add you.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            from openexecutive.api.routes.today import _build_today

            today = _build_today()
            lines: list[str] = ["**Today's Dashboard**"]
            if today.departments:
                lines.append("\n**Departments**")
                for d in today.departments:
                    flags = []
                    if d.at_risk_count:
                        flags.append(f"{d.at_risk_count} at-risk")
                    if d.off_track_count:
                        flags.append(f"{d.off_track_count} off-track")
                    if d.awaiting_count:
                        flags.append(f"{d.awaiting_count} waiting")
                    status = ", ".join(flags) if flags else "all clear"
                    lines.append(f"• **{d.title}** — {status}")
            if today.proposals:
                lines.append(f"\n**Pending Proposals**: {len(today.proposals)}")
            summary = "\n".join(lines)
        except Exception:
            logger.exception("Discord /today failed")
            summary = "Unable to load Today dashboard right now."

        for chunk in _split_message(summary):
            await interaction.followup.send(chunk)

    # ------------------------------------------------------------------ #
    # Gateway events
    # ------------------------------------------------------------------ #

    @bot.event
    async def on_ready() -> None:
        logger.info("Discord bot ready as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        guild_ids = settings.discord_guild_ids
        if guild_ids:
            for gid in guild_ids:
                guild = discord.Object(id=gid)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
            logger.info("Discord slash commands synced to %d guild(s)", len(guild_ids))
        else:
            await bot.tree.sync()
            logger.info("Discord slash commands synced globally")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author == bot.user:
            return
        # Roster gate happens inside _handle_message after the
        # integration_inbound audit row, so we get full observability
        # for rejected attempts.

        cleaned = _clean_message(message.content)
        # Allow attachment-only messages (no caption text) through the early
        # guard — we still need to process the files and respond.
        if not cleaned and not message.attachments:
            await bot.process_commands(message)
            return

        mode = _classify_inbound(message, bot.user)
        if mode == CLASSIFY_SKIP:
            await bot.process_commands(message)
            return

        is_dm = mode == CLASSIFY_DM
        discord_user_id = str(message.author.id)
        discord_channel = str(message.channel.id)
        message_id = str(message.id)

        # ``new_thread_id`` is kept as a None sentinel here because
        # _compute_session_id still accepts it; the previous eager
        # thread-creation path (CLASSIFY_MENTION → message.create_thread
        # up-front) is gone, and CLASSIFY_MENTION_CHANNEL handles its
        # own thread creation lazily inside the router.
        new_thread_id: str | None = None
        # ``response_router`` is wired only for the new plain-channel
        # @mention path (CLASSIFY_MENTION_CHANNEL). Other modes use the
        # eager ``send_fn`` chosen below.
        response_router: Callable[[str], Awaitable[MentionRouteResult]] | None = None

        if mode in (CLASSIFY_DM, CLASSIFY_THREAD_CONTINUATION):
            # Reply in the same channel — for DMs that's the DM, for thread
            # continuations that's the existing thread. Do NOT call
            # message.create_thread() inside a thread; Discord rejects it.
            async def send_fn(text: str) -> None:
                await message.channel.send(text)
        elif mode == CLASSIFY_MENTION:
            # @mention inside an existing (human-owned) thread — reply inline,
            # don't try to spawn a sub-thread (Discord rejects nested threads).
            async def send_fn(text: str) -> None:
                await message.channel.send(text)
        elif mode == CLASSIFY_MENTION_CHANNEL:
            # @mention in a plain TextChannel. Defer the thread-vs-inline
            # decision until after the reply is generated — see the
            # response_router below. The placeholder send_fn is used only
            # for the error path inside _handle_message (e.g.
            # executive.chat raised); the router replaces it before any
            # successful reply chunks are sent.
            async def send_fn(text: str) -> None:
                await message.reply(text)

            threshold_chars = settings.discord_mention_thread_threshold_chars
            reply_mode = settings.discord_mention_reply_mode
            channel_name = getattr(message.channel, "name", None) or "channel"
            author_name = (
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None)
                or "user"
            )
            # Snapshot the user's typed text BEFORE the attachment branch
            # below reassigns ``cleaned`` to prepend extracted document
            # text. The router is called later (after executive.chat
            # returns), so a naked ``cleaned`` reference here would late-
            # bind to the augmented value and misclassify "hi + PDF" as
            # not-a-greeting via the attachment content. The greeting
            # check should reflect what the human actually typed.
            user_text_for_routing = cleaned

            async def response_router(response_text: str) -> MentionRouteResult:
                # All four-way mode logic lives in _resolve_thread_promotion
                # so it can be unit-tested without instantiating a Bot.
                promote = _resolve_thread_promotion(
                    reply_mode=reply_mode,
                    user_text=user_text_for_routing,
                    response_text=response_text,
                    threshold_chars=threshold_chars,
                )

                if not promote:
                    async def inline_send(text: str) -> None:
                        await message.reply(text)
                    return MentionRouteResult(send_fn=inline_send)

                # Promote: create a brand-new bot-owned thread and route
                # the reply there. If thread creation fails, fall back to
                # an inline reply rather than dropping the answer on the
                # floor.
                try:
                    new_thread = await message.create_thread(
                        name=_THREAD_TITLE_PLACEHOLDER
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Discord: failed to create thread for @mention in "
                        "channel=%s — falling back to inline reply",
                        discord_channel,
                    )
                    async def inline_fallback(text: str) -> None:
                        await message.reply(text)
                    return MentionRouteResult(send_fn=inline_fallback)

                # Post a one-line pointer in the channel so people not
                # following the thread know where the reply went. Discord
                # auto-renders the thread reference as a clickable link.
                # If ``message.reply`` fails (the original message was
                # deleted, rate-limit, etc.), fall back to a plain
                # channel send so the channel still gets some pointer.
                # On both failures Discord still auto-emits a "started a
                # thread" system notification, so the channel is never
                # fully silent.
                pointer_text = f"↪ Replying in a thread: {new_thread.mention}"
                try:
                    await message.reply(pointer_text)
                except discord.HTTPException:
                    try:
                        await message.channel.send(pointer_text)
                    except discord.HTTPException:
                        logger.error(
                            "Discord: pointer reply + channel.send both failed "
                            "for promoted thread %s — channel readers will only "
                            "see Discord's auto 'started a thread' notification",
                            new_thread.id,
                        )

                async def thread_send(text: str) -> None:
                    await new_thread.send(text)

                return MentionRouteResult(
                    send_fn=thread_send,
                    promoted_thread_id=str(new_thread.id),
                    promoted_session_title=f"Discord #{channel_name} ({author_name})",
                    promoted_thread=new_thread,
                )

        else:
            # Defensive — _classify_inbound returned an unknown mode. Reply
            # inline so the user still gets an answer; no thread.
            async def send_fn(text: str) -> None:
                await message.reply(text)

        session_id = _compute_session_id(
            mode=mode,
            discord_user_id=discord_user_id,
            channel_id=discord_channel,
            new_thread_id=new_thread_id,
            fallback_message_id=message_id,
        )
        session_title = _session_title_for_message(message, mode)

        # Process file attachments — download, extract text / build image blocks.
        # Must run BEFORE the send_fn closure is called so we can prepend
        # extracted text to `cleaned` and pass image_blocks through.
        #
        # Roster-gated separately here, because that "before" put it before
        # _handle_message's gate too: an unrostered sender's message was
        # dropped, but not until after we had downloaded their file and
        # indexed its text.
        #
        # The message still goes to _handle_message, so a rejected attempt
        # still produces both the integration_inbound row and the
        # rejected_unknown_sender row. What changes is that row's preview:
        # extracted attachment text used to be prepended to `cleaned` and so
        # appeared in it. Not extracting an unrostered sender's file is the
        # point, and `attachments` below records that one was sent.
        att_image_blocks: list[dict] = []
        if message.attachments and _is_rostered(discord_user_id):
            try:
                from openexecutive.integrations.attachments import (
                    AttachmentItem,
                    process_attachments,
                )

                att_text, att_image_blocks = await process_attachments([
                    AttachmentItem(
                        url=a.url,
                        filename=a.filename,
                        content_type=a.content_type or "",
                        size=a.size,
                    )
                    for a in message.attachments
                ])
                if att_text:
                    cleaned = (f"{att_text}\n\n{cleaned}").strip() if cleaned else att_text
            except Exception:
                logger.exception("Discord: attachment processing failed")

        # No eager `on_first_turn` rename hook needed anymore.
        # CLASSIFY_MENTION_CHANNEL's lazy router schedules its own rename
        # via _schedule_thread_rename after a successful promote, and the
        # other modes (DM, CLASSIFY_MENTION inside an existing thread,
        # CLASSIFY_THREAD_CONTINUATION) never create a brand-new bot-owned
        # thread that needs renaming.
        on_first_turn: Callable[[str, str], Awaitable[None]] | None = None

        # Gate eligibility: only for auto-reply in a bot-owned thread (no
        # re-tag). DMs are always answered; explicit @mentions are explicit
        # by definition and bypass the gate.
        gate_eligible = (
            mode == CLASSIFY_THREAD_CONTINUATION
            and not _is_direct_mention(message, bot.user)
        )

        # Multi-peer: in a thread, fetch the participant list so Honcho
        # can add every co-present human as a peer in the session. DMs
        # are 1:1 — skip. Best-effort: any discord.py API hiccup
        # degrades to an empty list rather than blocking the turn.
        co_present_discord_user_ids: list[str] = []
        if not is_dm and isinstance(message.channel, discord.Thread):
            try:
                # Cap at 200 — same bound Slack uses for conversations_replies.
                # Threads can technically hold up to 1000 members; an
                # unbounded fetch_members() per turn would add seconds of
                # paginated HTTP roundtrips for populated threads. Honcho
                # peer-of-peer reasoning saturates well before 200 anyway.
                # Requires the privileged `members` intent in the Dev
                # Portal; without it discord.py returns an empty list.
                thread_members = await message.channel.fetch_members()
                # Pre-compute the bot's own user id so we can exclude it
                # from the co-present list. (`bot.user` is None during
                # boot before the gateway handshake; tolerate that.)
                bot_user_id_str = str(bot.user.id) if bot.user else ""
                co_present_discord_user_ids = [
                    str(m.id)
                    for m in thread_members[:200]
                    if str(m.id) != bot_user_id_str
                ]
            except Exception:
                logger.warning(
                    "Discord: fetch_members failed for thread %s — passing empty co-present list",
                    message.channel.id,
                    exc_info=True,
                )

        # Use the channel id as thread_id so WaitForHuman resolution can match
        # replies that arrive in the same DM or thread channel.
        await _handle_message(
            text=cleaned,
            attachment_count=len(message.attachments or []),
            discord_user_id=discord_user_id,
            discord_channel=discord_channel,
            message_id=message_id,
            thread_id=str(message.channel.id),
            send_fn=send_fn,
            is_dm=is_dm,
            session_id=session_id,
            session_title=session_title,
            on_first_turn_complete=on_first_turn,
            author_display_name=getattr(message.author, "display_name", None),
            gate_eligible=gate_eligible,
            bot_display_name=getattr(bot.user, "display_name", None) if bot.user else None,
            attachment_blocks=att_image_blocks or None,
            co_present_discord_user_ids=co_present_discord_user_ids or None,
            response_router=response_router,
        )

        await bot.process_commands(message)

    return bot


async def send_dm(discord_user_id: str, text: str) -> str | None:
    """Send a Discord direct message to a specific user.

    Opens (or fetches) the user's DM channel via POST /users/@me/channels,
    then posts the text — chunked through `_split_message()` so messages
    longer than Discord's per-message limit still deliver.

    Returns the Discord message id of the last chunk sent (best-effort —
    ``None`` if the response lacks an id), so callers can link the outbound
    message to a later reply. Unlike `post_notification()` (which swallows
    errors as best-effort notifications), this helper raises so the calling
    tool handler can surface a structured error to the Executive. Mirrors the
    contract of `send_message()` in telegram_bot.py.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    if not settings.discord_bot_token:
        raise RuntimeError("discord bot token is not configured")
    if not discord_user_id:
        raise ValueError("discord_user_id is required")

    import httpx

    headers = {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "Content-Type": "application/json",
    }
    last_message_id: str | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Open / fetch the DM channel. Discord returns the same channel id
        # on repeat calls for the same recipient — safe to call every send.
        open_resp = await client.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers,
            json={"recipient_id": discord_user_id},
        )
        open_resp.raise_for_status()
        channel_id = open_resp.json().get("id")
        if not channel_id:
            raise RuntimeError("discord did not return a DM channel id")

        for chunk in _split_message(text):
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": chunk},
            )
            resp.raise_for_status()
            try:
                body = resp.json()
                last_message_id = body.get("id") or last_message_id
            except (ValueError, TypeError, AttributeError):
                pass
    return last_message_id


async def send_channel_message(channel_id: str, text: str) -> None:
    """Send a Discord channel message, raising on failure.

    Parallel to `send_dm` but targeting a channel id (server text channel,
    thread, or DM channel id) rather than a user id. Used by the broadcast
    tools where the Executive needs a structured error on failure rather
    than the silent-best-effort behaviour of `post_notification`.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    if not settings.discord_bot_token:
        raise RuntimeError("discord bot token is not configured")
    if not channel_id:
        raise ValueError("channel_id is required")

    import httpx

    headers = {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for chunk in _split_message(text):
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": chunk},
            )
            resp.raise_for_status()


async def post_notification(text: str, channel_id: int | None = None) -> None:
    """Send an outbound notification to a Discord channel.

    Uses a short-lived HTTP request to the Discord REST API so this
    helper works from any process without a running gateway connection.
    Channel falls back to DISCORD_NOTIFY_CHANNEL_ID if channel_id is None.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    target = channel_id or settings.discord_notify_channel_id
    if not target or not settings.discord_bot_token:
        logger.warning("Discord post_notification: no channel or token configured, skipping")
        return

    try:
        import httpx

        headers = {
            "Authorization": f"Bot {settings.discord_bot_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            for chunk in _split_message(text):
                resp = await client.post(
                    f"https://discord.com/api/v10/channels/{target}/messages",
                    headers=headers,
                    json={"content": chunk},
                )
                resp.raise_for_status()
    except Exception:
        logger.exception("Discord post_notification failed")


def run_discord_bot() -> None:
    from openexecutive.config import get_settings

    settings = get_settings()
    bot = create_discord_bot()
    logger.info("Starting Discord bot...")
    asyncio.run(bot.start(settings.discord_bot_token))  # type: ignore[arg-type]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_discord_bot()
