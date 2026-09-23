from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import time
import uuid
from typing import Any, NamedTuple

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from openexecutive.api.models import ChatRequest, PageContext, StopChatRequest
from openexecutive.audit import log_event as audit_log
from openexecutive.integrations.attachments import build_attachment_output
from openexecutive.orchestrator.debug_events import DebugCollector

# Per-file size cap. Mirrors `_DEFAULT_MAX_BYTES` in
# `openexecutive/integrations/attachments.py` so the web chat behaves the same
# as Discord / Telegram uploads.
_MAX_BYTES_PER_FILE = 20 * 1024 * 1024
# Per-turn file count cap. Beyond a handful the user is bulk-ingesting and
# should hit POST /documents instead.
_MAX_FILES_PER_TURN = 5

router = APIRouter()
logger = logging.getLogger(__name__)

_sessions: dict[str, Any] = {}

# Most-recent completed turn's debug events, for the /debug/last-turn endpoint.
# Single-process only; replaced wholesale at the end of every turn.
_last_turn_events: list[dict[str, Any]] = []
_last_turn_meta: dict[str, Any] = {}

_TITLE_MAX_LEN = 60


# Session ids are client-supplied (a JSON field on /chat, a form field on
# /chat/upload) and are now bound as the audit session for the WHOLE turn, so
# they reach every audit and usage row the turn produces — and the route's own
# log lines. An unconstrained value is therefore a log-injection vector (a
# newline forges a log record) and makes mis-attribution trivially easy.
# Same charset as api.routes.audit._SESSION_ID_RE, which already had to
# defend the read side against integration-derived ids; it is wide enough for
# every real form ("slack:thread:C1:1700000000.001", "email:x@host", uuid4).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_:@\-\.\+/=]{1,256}$")


def _clean_session_id(session_id: str | None) -> str | None:
    """Drop a client-supplied session id that isn't a plausible id.

    Dropped rather than rejected: a malformed id is indistinguishable from a
    stale client, and minting a fresh session keeps the turn working while
    denying the attacker a chosen audit key.
    """
    if session_id is None:
        return None
    if not _SESSION_ID_RE.match(session_id):
        logger.warning(
            "chat.session_id_rejected len=%d", len(session_id),
        )
        return None
    return session_id


# Client-minted turn ids address an in-flight turn from POST /chat/stop. The
# charset is deliberately NARROWER than _SESSION_ID_RE: this value reaches log
# lines (same log-injection argument as above) and nothing here needs the
# integration-shaped ids a session id has to carry — a uuid4 is the only
# expected shape.
# `\Z`, not `$`: `$` also matches before a trailing newline, so `re.match`
# would accept "aaaaaaaa\n" and register an id with a newline in it.
_CLIENT_TURN_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}\Z")

# Upper bound on the live-turn registry. Entries are popped in _sse_body's
# `finally`, so this only matters if a turn dies somewhere that bypasses it;
# the cap keeps such a leak from growing without limit.
_STOP_REGISTRY_MAX = 512

# Headroom on top of the whole-turn deadline before a registry entry is
# considered stranded. Covers the pre-stream context fetch and the post-stream
# persistence, neither of which counts against the turn deadline.
_STOP_TTL_GRACE_S = 120.0


class _StopEntry(NamedTuple):
    event: asyncio.Event
    # Who may stop this turn. NOT the raw `caller_person_id`: that resolves to
    # None for a signed-in user who isn't on the People roster yet, for a fresh
    # install with no principal, and on any transient DB error during lookup —
    # so comparing person ids directly would make every such caller the owner
    # of every other such caller's turn. See `_stop_owner_key`.
    owner: str
    turn_id: str
    # Monotonic registration time, used to reclaim entries stranded by a path
    # that never reaches `_sse_body`'s `finally`.
    started_at: float


# client_turn_id -> stop switch for every currently-streaming chat turn. Same
# shape as `_active_cancellations` in api/routes/evals.py — structurally the
# identical mechanism (id -> event registry, a POST that flips it, a terminal
# SSE event, the streamer popping the entry in its `finally`).
#
# The vocabulary differs on purpose and the split is worth knowing about when
# grepping: evals "cancels" a RUN the user started and may not be watching, and
# says `canceled`; chat "stops" a reply that is being written in front of the
# user, and says `stopped`, because that is the word on the button. If you are
# looking for every stoppable-async pattern in this codebase, grep both.
#
# Single-process only — exactly like `_sessions` above and `_last_turn_*` below. A second
# uvicorn worker would break in-flight session continuity before it broke this.
_active_stops: dict[str, _StopEntry] = {}


def _clean_client_turn_id(client_turn_id: str | None) -> str | None:
    """Drop a client-supplied turn id that isn't a plausible id.

    Dropped rather than rejected, following `_clean_session_id`: a malformed id
    only means this turn cannot be stopped, which is never a reason to fail the
    turn itself.
    """
    if client_turn_id is None:
        return None
    if not _CLIENT_TURN_ID_RE.match(client_turn_id):
        logger.warning("chat.client_turn_id_rejected len=%d", len(client_turn_id))
        return None
    return client_turn_id


# Strong references to detached rename tasks, so GC cannot cancel one
# mid-flight. Mirrors `memory.episodic._background_tasks`.
_rename_tasks: set[asyncio.Task[None]] = set()


def _rename_session_in_background(
    session_id: str, message: str, full_response: str, turn_id: str
) -> None:
    """Generate and store a session title without holding up the stream.

    Used on the stop path only. Everywhere else the rename is awaited so the
    sidebar has the good title by the time the client acts on `done`.
    """
    from openexecutive.config import get_settings
    from openexecutive.memory.session_store import update_session_title
    from openexecutive.utils.session_title import generate_session_title

    async def _run() -> None:
        try:
            new_title = await asyncio.wait_for(
                generate_session_title(message, full_response),
                timeout=get_settings().utility_fast_timeout_s,
            )
            if new_title:
                update_session_title(session_id, new_title)
        except Exception:
            # asyncio.TimeoutError is an Exception subclass in 3.11+, so this
            # covers the timeout and any DB failure. A missed rename only
            # leaves the placeholder title.
            logger.exception("chat.title_update_failed turn_id=%s", turn_id)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:  # pragma: no cover - no running loop (CLI context)
        return
    _rename_tasks.add(task)
    task.add_done_callback(_rename_tasks.discard)


def _stop_owner_key(request: Request, caller_person_id: int | None) -> str:
    """A stable identity for "who may stop this turn".

    Prefers the roster Person id, then the verified caller email, then a
    local-trust sentinel. The email step matters: `_resolve_caller_person_id`
    collapses every unrostered signed-in user to None, and two different people
    must not end up owning each other's turns just because neither is on the
    roster yet. The UI proxy strips client-sent `x-caller-*` and re-stamps the
    header from the verified session, so the email cannot be spoofed.

    The sentinel is only reached when there is no header at all — CLI and
    direct curl against a local API, where there is no identity to separate.
    """
    if caller_person_id is not None:
        return f"person:{caller_person_id}"
    email = (request.headers.get("x-caller-email") or "").strip().lower()
    if email:
        return f"email:{email}"
    return "local"


def _sweep_stale_stops() -> None:
    """Reclaim entries that outlived any turn that could still be running.

    Entries are normally popped in `_sse_body`'s `finally`, but that only runs
    once Starlette starts consuming the generator. Anything that strands an
    entry would otherwise hold its slot for the life of the process — and since
    a full registry now refuses NEW turns rather than evicting live ones, 512
    strandings would silently take the Stop button away from everybody until a
    restart. The TTL is generous: it only needs to exceed the longest a turn
    can legitimately hold a slot.
    """
    # Imported lazily, like every other `get_settings` use in this module.
    from openexecutive.config import get_settings

    settings = get_settings()
    ttl = (
        settings.chat_stream_timeout_s
        + settings.committee_extra_timeout_s
        + _STOP_TTL_GRACE_S
    )
    cutoff = time.monotonic() - ttl
    stale = [k for k, e in _active_stops.items() if e.started_at < cutoff]
    for key in stale:
        _active_stops.pop(key, None)
    if stale:
        logger.warning("chat.stop_registry_swept count=%d", len(stale))


def _register_stop(
    client_turn_id: str, owner: str, turn_id: str
) -> asyncio.Event | None:
    """Register a stop switch for a turn that is about to stream.

    Returns None when the turn could not be registered, which only costs it the
    Stop button — never the turn itself.
    """
    _sweep_stale_stops()
    if client_turn_id in _active_stops:
        # A live turn already owns this id (client retry, double submit, a
        # scripted caller reusing a value). Overwriting would make the first
        # turn permanently unstoppable and point Stop at the wrong one, and
        # whichever finished first would pop the other's entry.
        logger.warning("chat.stop_id_collision turn_id=%s", turn_id)
        return None
    if len(_active_stops) >= _STOP_REGISTRY_MAX:
        # Deliberately refuse the NEW turn rather than evicting the oldest
        # entry: entries are popped when their turn ends, so the oldest is
        # typically a long-running LIVE turn, and evicting it would silently
        # take the Stop button away from the person most likely to want it.
        logger.warning("chat.stop_registry_full turn_id=%s", turn_id)
        return None
    event = asyncio.Event()
    _active_stops[client_turn_id] = _StopEntry(
        event, owner, turn_id, time.monotonic()
    )
    return event


def _release_stop(client_turn_id: str | None, turn_id: str | None = None) -> None:
    """Drop a turn's registry entry, but only if it is still that turn's.

    The identity check stops one turn from popping an entry belonging to a
    different turn that reused the same client id.
    """
    if not client_turn_id:
        return
    entry = _active_stops.get(client_turn_id)
    if entry is None:
        return
    if turn_id is not None and entry.turn_id != turn_id:
        return
    _active_stops.pop(client_turn_id, None)


class _StreamStep(NamedTuple):
    """One step of the SSE driver loop.

    Exactly one of these is true: `item` holds a produced value, or `exhausted`
    / `stopped` / `timed_out` says why nothing was produced.
    """

    item: Any = None
    produced: bool = False
    exhausted: bool = False
    stopped: bool = False
    timed_out: bool = False


async def _cancel_stream_step(anext_task: asyncio.Task[Any], turn_id: str) -> None:
    """Cancel an in-flight `__anext__` step and wait for it to unwind.

    `cancel()` only *requests* cancellation; the throw is not delivered until
    the task next runs. Two things depend on actually awaiting it:

    - `aclose()` on an async generator whose `__anext__` task is still running
      raises RuntimeError("asynchronous generator is already running").
    - The await is what stops the work. The CancelledError propagates into
      whatever the Executive is awaiting — the Anthropic stream, a tool call, a
      specialist gather — which is why executive.py re-raises CancelledError
      out of its `gather(return_exceptions=True)` calls rather than treating it
      as a tool failure.
    """
    if anext_task.done():
        return
    anext_task.cancel()
    try:
        await anext_task
    except (asyncio.CancelledError, StopAsyncIteration):
        # Deliberately not `suppress(BaseException)`: that would hide real
        # cleanup errors. Catching CancelledError here can also swallow an
        # OUTER cancel re-delivered during this await, but we are called from a
        # `finally` while that cancellation is already propagating, so it
        # continues on its way regardless — and the inner `cancel()` above has
        # already been requested, which is the part that must not be skipped.
        pass
    except Exception:
        logger.exception("chat.stream_step_failed turn_id=%s", turn_id)


async def _next_stream_step(
    stream: Any,
    stop_waiter: asyncio.Task[bool] | None,
    remaining: float,
    turn_id: str,
) -> _StreamStep:
    """Advance `stream` one step, racing the stop switch and the deadline.

    Extracted from the driver loop because the ordering here is load-bearing in
    several ways and is much easier to reason about — and to test — on its own.

    `asyncio.wait_for` cannot be used: it owns its inner task and cancels it on
    timeout, leaving nothing to race a second future against. But that also
    means we inherit a duty `wait_for` used to discharge for us. `asyncio.wait`
    does NOT cancel its futures when the task awaiting it is cancelled, and
    Starlette cancels this whole body on `http.disconnect` — so without the
    `finally` below, a closed tab would leave `__anext__` running a full
    specialist or tool round with no deadline, no registry entry and no
    persistence. The `finally` is the load-bearing part of this function.
    """
    # One Task per step — exactly what `asyncio.wait_for` built here before,
    # and like it this COPIES the current context, so the `set_turn` /
    # `set_session` bindings made in `event_generator` are inherited by every
    # step. See that function's comment for why that matters.
    anext_task: asyncio.Task[Any] = asyncio.ensure_future(stream.__anext__())
    try:
        waiters: set[asyncio.Future[Any]] = {anext_task}
        if stop_waiter is not None:
            waiters.add(stop_waiter)
        await asyncio.wait(
            waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )

        if anext_task.done():
            # Deliver a produced item even when the stop fired in the same
            # tick. The work is already paid for, and an `action_taken` here
            # would otherwise be dropped from the chips we persist. There is no
            # await between the `wait` above and this check, so nothing can slip
            # in between them.
            try:
                return _StreamStep(item=anext_task.result(), produced=True)
            except StopAsyncIteration:
                return _StreamStep(exhausted=True)

        if stop_waiter is not None and stop_waiter.done():
            return _StreamStep(stopped=True)
        return _StreamStep(timed_out=True)
    finally:
        # Every exit: stop, deadline, AND an outer cancellation from a client
        # disconnect. Never leave the step running.
        await _cancel_stream_step(anext_task, turn_id)


def _request_stop(client_turn_id: str, owner: str) -> str | None:
    """Flip a turn's stop switch. Returns its server `turn_id`, or None.

    None covers BOTH "no such live turn" and "not yours" on purpose: the two
    must be indistinguishable to the caller, or the endpoint becomes an oracle
    for which turn ids are live.
    """
    entry = _active_stops.get(client_turn_id)
    if entry is None or entry.owner != owner:
        return None
    entry.event.set()
    return entry.turn_id


def _get_or_create_session(session_id: str | None, request: Request) -> Any:
    from openexecutive.memory.session_store import load_messages
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.session import Session

    session_id = _clean_session_id(session_id)
    if session_id and session_id in _sessions:
        return _sessions[session_id]

    new_id = session_id or str(uuid.uuid4())
    profile = load_or_create_profile()
    session = Session(
        session_id=new_id,
        company_profile=profile if not profile.is_empty() else None,
        from_web_chat=True,
    )

    if session_id:
        # Server may have restarted — reload history from DB so conversation continues.
        history = load_messages(session_id)
        if history:
            session.conversation_history = history

    _sessions[new_id] = session
    return session


def _extract_sources(text: str) -> list[str]:
    """Extract unique [filename] source prefixes embedded by the retriever."""
    return list(dict.fromkeys(re.findall(r"\[([^\]]+)\]", text)))


# Cap on the serialized FORM ON SCREEN descriptor so a pathological client
# can't balloon the user turn. ~12k chars comfortably fits the workflow
# builder (the largest form) with room to spare.
_PAGE_FORM_JSON_MAX_CHARS = 12_000


def _build_page_context_block(page_context: PageContext | None) -> str:
    """Render the Ask OE panel's page context for the user turn.

    Pure string builder (unit-testable, no I/O beyond the prebuilt guide
    file read). Returns "" when no page context was sent, so the main chat
    page and integration channels are byte-identical to before.
    """
    if page_context is None:
        return ""

    lines: list[str] = [f'PAGE: {page_context.route} — "{page_context.title}"']
    if page_context.summary:
        lines.append(page_context.summary)

    if page_context.guide_section_id:
        from openexecutive.guide.prebuilt import get_prebuilt

        section = get_prebuilt(page_context.guide_section_id)
        markdown = (section or {}).get("markdown", "")
        if markdown:
            lines.append("")
            lines.append(
                f"USER GUIDE FOR THIS PAGE ({page_context.guide_section_id}):"
            )
            lines.append(markdown)

    if page_context.form is not None:
        form = page_context.form
        form_json = form.model_dump_json()
        if len(form_json) > _PAGE_FORM_JSON_MAX_CHARS:
            logger.warning(
                "page_context form descriptor truncated: form_id=%s chars=%d",
                form.form_id,
                len(form_json),
            )
            form_json = form_json[:_PAGE_FORM_JSON_MAX_CHARS] + "…[truncated]"
        lines.append("")
        lines.append(f'FORM ON SCREEN (form_id={form.form_id}): "{form.title}"')
        lines.append(form_json)
        lines.append("")
        lines.append(
            "If the user asks you to fill, change, or set up this form, call "
            f"propose_form_values with form_id={form.form_id!r} and a `fields` "
            "object whose keys are the field names listed above. Do not save "
            "or submit anything — the user reviews the suggested values and "
            "saves manually."
        )

    return "\n".join(lines)


def _seed_seen_channel_refs(session: Any, person_id: int | None) -> None:
    """Record the caller's OWN channel addresses as seen on this session.

    `schedule_followup` refuses to queue a send to a `(channel, channel_ref)`
    the session has not seen — an anti-spam gate, and the reason an injected
    "email this to attacker@evil" in an alert body cannot become a scheduled
    send. The chat adapters populate it from the inbound message; nothing
    populated it for a browser turn.

    The gate was unreachable on web until `set_session` bound the session for
    the whole stream, so it silently allowed ANY address; reachable, it reads
    an empty set as "seen nothing" and refuses even the principal's own,
    breaking "remind me tomorrow at 9am". Seeding the caller's own refs is
    what makes the gate mean on web what it means everywhere else: you may
    schedule to yourself, and to whatever this conversation actually used.

    Adds only. An address removed from the roster stays in the set for the
    life of the in-memory session.
    """
    if person_id is None:
        return
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
    except Exception:
        logger.warning("chat.seen_refs_seed_failed person_id=%s", person_id, exc_info=True)
        return
    if person is None:
        return
    for channel, ref in (
        ("email", getattr(person, "email", None)),
        ("slack_dm", getattr(person, "slack_user_id", None)),
        ("discord_dm", getattr(person, "discord_user_id", None)),
        ("telegram", getattr(person, "telegram_chat_id", None)),
    ):
        if ref:
            session.seen_channel_refs.add((channel, str(ref)))


def _resolve_caller_person_id(request: Request) -> int | None:
    """Resolve the calling Person from the `x-caller-email` header.

    Precedence is deliberately strict to prevent cross-identity data leaks:
      - header present + match → that Person's id.
      - header present + no match → None (signed-in but unrostered users
        must not be silently fused with the principal's data).
      - header absent → fall back to the principal (CLI / direct curl,
        plus channel paths without the header).
    """
    caller_email = (request.headers.get("x-caller-email") or "").strip().lower()
    try:
        from openexecutive.people.store import (
            find_person_by_email,
            find_principal_person,
        )
        if caller_email:
            caller = find_person_by_email(caller_email)
            return caller.id if caller is not None else None
        principal = find_principal_person()
        return principal.id if principal is not None else None
    except (OSError, sqlite3.Error) as exc:
        logger.warning("caller_lookup_failed err=%s", exc)
        return None


async def _run_chat_turn(
    *,
    message: str,
    session_id: str | None,
    committee_review: bool,
    attachment_blocks: list[dict[str, Any]] | None,
    request: Request,
    page_context: PageContext | None = None,
    client_turn_id: str | None = None,
    memory_text: str | None = None,
) -> StreamingResponse:
    """Shared streaming-chat handler for both the JSON and multipart routes.

    `message` is the (already attachment-augmented) text the Executive will
    see and that we persist as the user turn. `attachment_blocks` carries
    image content blocks; document text is expected to already be inlined in
    `message` by the caller. `memory_text`, when set, is what peer memory
    records as the caller's words instead of `message` (see
    `Executive.stream_chat`).
    """
    from openexecutive.config import get_settings
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.memory.session_store import create_session
    from openexecutive.orchestrator.executive import Executive

    t0 = time.monotonic()
    # Same shape every other entry point mints (Executive.stream_chat), so
    # an audit query filtering on the `t-` prefix sees SSE turns too.
    turn_id = f"t-{uuid.uuid4().hex[:12]}"
    collector = DebugCollector(t0=t0, turn_id=turn_id)

    # Resolve the caller and arm the stop switch FIRST, before the session
    # load, the audit write and the context fan-out below. Every one of those
    # happens before the StreamingResponse exists, so the client has no SSE
    # byte to learn a server id from and can only address the turn by the id it
    # minted itself. Registering here makes the Stop button live from as close
    # to "the moment Send was pressed" as the server can manage; a stop landing
    # in this window also costs zero tokens, because the Executive is never
    # asked for a first step.
    #
    # `_resolve_caller_person_id` is used for Honcho's per-person memory and
    # for tagging the session row's owner (so /sessions can filter the sidebar
    # by signed-in user); see its docstring for the precedence rule that
    # protects against cross-identity leakage.
    caller_person_id = _resolve_caller_person_id(request)
    client_turn_id = _clean_client_turn_id(client_turn_id)
    stop_event: asyncio.Event | None = None
    if client_turn_id:
        stop_event = _register_stop(
            client_turn_id, _stop_owner_key(request, caller_person_id), turn_id
        )
        if stop_event is None:
            # Refused (id collision, or the registry is at its cap). The turn
            # still runs; it just isn't stoppable. Drop our handle so the
            # `finally` below can't pop an entry belonging to another turn.
            client_turn_id = None
        else:
            logger.info("chat.stop_registered turn_id=%s", turn_id)

    # Guarded for the same reason as the gather further down: the stop switch
    # is already registered, and `_sse_body`'s `finally` — which normally
    # releases it — only runs once Starlette starts consuming the generator.
    # This call can realistically raise (a hand-edited `company/profile.yaml`
    # that no longer parses, or a locked SQLite DB), and a stranded entry is
    # now permanent-ish: the registry refuses new turns at its cap rather than
    # evicting live ones, so enough of them would disable Stop for everybody.
    try:
        session = _get_or_create_session(session_id, request)
    except BaseException:
        _release_stop(client_turn_id, turn_id)
        raise
    is_first_turn = len(session.conversation_history) == 0

    logger.info(
        "chat.turn_start turn_id=%s session_id=%s is_first_turn=%s msg_len=%d attachments=%d",
        turn_id, session.session_id, is_first_turn, len(message),
        len(attachment_blocks or []),
    )
    audit_log(
        "chat_turn",
        f"User: {message[:200]}",
        session_id=session.session_id,
        turn_id=turn_id,
        actor="user",
        details={
            "direction": "in",
            "msg_len": len(message),
            "is_first_turn": is_first_turn,
            "attachment_count": len(attachment_blocks or []),
        },
        # memory_text reaches the caller's peer AND every consulted
        # department's shared memory, but never the transcript: keep it
        # auditable next to the message it stood in for.
        full={"message": message, "memory_text": memory_text},
    )

    # Let the caller schedule to their own addresses. Runs per turn rather than
    # only on session creation so a newly-added address works without a fresh
    # session; note it only ever ADDS, so an address removed from the roster
    # stays scheduleable for the life of this in-memory session.
    _seed_seen_channel_refs(session, caller_person_id)

    # Persist the session row immediately (idempotent INSERT OR IGNORE) so a
    # mid-turn failure never leaves a ghost in-memory session with no DB row.
    # `caller_person_id` is bound here on the first turn; INSERT OR IGNORE means
    # subsequent turns can't overwrite the owner.
    title = message[:_TITLE_MAX_LEN].replace("\n", " ") if is_first_turn else session.session_id
    try:
        create_session(
            session.session_id,
            title,
            session.created_at.isoformat(),
            caller_person_id=caller_person_id,
        )
    except Exception:  # pragma: no cover - DB write should not block the turn
        logger.exception("chat.session_persist_failed turn_id=%s", turn_id)

    # Fan out the three context-fetching steps in parallel. Each one is a few
    # hundred ms to a few seconds on its own and they are mutually independent,
    # so the previous serial layout was paying the sum of their latencies on
    # every turn:
    #   retrieve()          — ChromaDB query (sync, wrapped in to_thread)
    #   format_for_prompt() — SQLite read   (sync, wrapped in to_thread)
    #   _honcho_prefetch()  — Honcho HTTP roundtrip (async; ~3s avg, 3s budget)
    # The prefetch helper already swallows its own exceptions and returns ""
    # on timeout, so failure modes are unchanged.
    from openexecutive.audit import set_turn
    from openexecutive.memory.honcho_client import prefetch as _honcho_prefetch
    from openexecutive.orchestrator.schedule_tools import set_session

    async def _do_episodic() -> str:
        try:
            return await asyncio.to_thread(format_for_prompt)
        except Exception:
            logger.exception("chat.format_for_prompt_failed turn_id=%s", turn_id)
            return ""

    async def _do_briefing() -> str:
        # Current open-alert digest so the Executive can discuss a briefing item
        # the principal clicked or named (the items behind the /today "What's
        # going on" narrative). Sync SQLite read off the event loop; the
        # formatter swallows its own errors, so this is belt-and-suspenders.
        from openexecutive.briefing.context import render_and_trust

        # `render_and_trust` also records on the session exactly which alert
        # ids this block named. `ack_alert` accepts nothing else, so a web turn
        # that skipped this step could not clear a card at all — and, before
        # the trusted set was recorded here, could clear ANY id, including one
        # an inbound email wrote into an alert body.
        # return_exceptions=True so a digest raising never discards the others —
        # each formatter already swallows its own errors, this just guards the
        # to_thread wrappers themselves.
        results = await asyncio.gather(
            asyncio.to_thread(render_and_trust, session),
            return_exceptions=True,
        )
        digests: list[str] = []
        for res in results:
            if isinstance(res, BaseException):
                logger.exception(
                    "chat.briefing_context_failed turn_id=%s", turn_id, exc_info=res
                )
            elif res:
                digests.append(res)
        return "\n\n".join(digests)

    async def _do_prefetch() -> str:
        # Bind the audit ContextVars for THIS task so the peer_memory row the
        # prefetch wrapper emits is correctly linked to session_id / turn_id.
        # Without this the audit row lands with NULL session/turn (the prefetch
        # used to run inside stream_chat's `with set_turn(...)` block; moving it
        # to the route detached it from that context).
        with set_turn(session_id=session.session_id, turn_id=turn_id):
            try:
                return await _honcho_prefetch(
                    message,
                    person_id=caller_person_id,
                    session_id=session.session_id,
                    # Dialectic mode only; representation mode ignores it.
                    reasoning_level="medium" if committee_review else "low",
                )
            except Exception:
                # The wrapper itself catches and returns "" on timeout/error,
                # so this is belt-and-suspenders — but if a future change to
                # the helper raises, don't sink the whole gather() with it.
                logger.exception("chat.honcho_prefetch_failed turn_id=%s", turn_id)
                return ""

    # Clear the trusted alert ids BEFORE the gather, unconditionally. Web
    # sessions are long-lived (`_sessions`), so if `render_and_trust` never
    # runs this turn — the to_thread wrapper fails to schedule, the gather is
    # cancelled, a future code path skips the digest — the previous turn's set
    # would otherwise still be sitting there and `ack_alert` would accept it.
    # Clearing here makes "shown nothing, can ack nothing" hold on every path
    # instead of only the ones that reach the recorder.
    session.trusted_alert_ids = set()

    # `_sse_body`'s `finally` is what normally releases the registry entry, but
    # it only runs once Starlette starts consuming the generator. Everything
    # from here to the `StreamingResponse` below therefore needs its own
    # guard — the gather, the page-context builder and the settings load can
    # all raise, and the entry would otherwise be stranded until the registry
    # cap evicted it.
    try:
        retrieved_context, episodic_context, peer_memory_context, briefing_context = await asyncio.gather(
            asyncio.to_thread(
                retrieve,
                query=message,
                specialist_name=None,
                store=request.app.state.store if hasattr(request.app.state, "store") else None,
            ),
            _do_episodic(),
            _do_prefetch(),
            _do_briefing(),
        )
        sources = _extract_sources(retrieved_context)
        collector.emit("knowledge_retrieved", {
            "query": message,
            "chunk_count": len(sources),
            "sources": sources,
        })
        logger.info("chat.knowledge_done turn_id=%s chunks=%d", turn_id, len(sources))

        page_context_block = _build_page_context_block(page_context)

        executive = Executive(
            mcp_gateway=getattr(request.app.state, "mcp_gateway", None)
        )
        settings = get_settings()
        timeout_s = settings.chat_stream_timeout_s

        if committee_review:
            # Committee adds reviewer fan-out + a full revision pass on top of
            # the draft. Extend the whole-turn deadline so the route doesn't
            # cut us off mid-revision.
            timeout_s += settings.committee_extra_timeout_s
    except BaseException:
        _release_stop(client_turn_id, turn_id)
        raise

    async def event_generator():
        # Bind the turn AND the session for the whole SSE body, not just its
        # first step. `_sse_body` drives the executive with `asyncio.wait_for`,
        # which wraps every `__anext__()` in a fresh Task that copies the
        # context at that moment — so a binding made *inside* the executive's
        # own generator lands in a throwaway per-step context and is gone by
        # the next resume. Everything after step one (the whole tool-call loop
        # and the specialist fan-out) then records with no session or turn.
        # Bound out here, in the generator Starlette itself drives, every
        # step inherits it. Both managers save and restore rather than using
        # Token.reset precisely so they survive that task-hopping.
        #
        # `set_session` is here for exactly the same reason `set_turn` is, and
        # was missing until it cost a production incident — see its docstring
        # in `orchestrator.schedule_tools` for the mechanism and the blast
        # radius. Every tool handler that reads `current_session` mid-turn
        # depends on this binding.
        # aclosing is load-bearing, not decoration: `async for` does NOT
        # close its sub-iterator when the enclosing generator is closed. This
        # body used to BE event_generator, so a client disconnect ran its
        # `finally` directly (the /debug/last-turn snapshot, and the
        # Executive's upstream stream aclose). Wrapping it in a plain
        # `async for` would leave `_sse_body` suspended at its yield until a
        # later GC hop — or never, if the loop closes first.
        with (
            set_turn(session_id=session.session_id, turn_id=turn_id),
            set_session(session),
        ):
            async with contextlib.aclosing(_sse_body()) as body:
                async for evt in body:
                    yield evt

    async def _sse_body():
        from openexecutive.memory.session_store import (
            save_message,
            update_session_timestamp,
        )

        full_response = ""
        # Row id of the persisted assistant reply, handed to the client on
        # `done` so it can attach 👍/👎 without a second roundtrip.
        persisted: dict[str, int] = {}
        chunk_count = 0
        exec_t0 = time.monotonic()
        timed_out = False
        client_disconnected = False
        stopped = False

        try:
            # Flush knowledge_retrieved (and any other pre-stream events) first.
            for evt in collector._events:
                yield f"data: {json.dumps(collector.to_sse_dict(evt))}\n\n"

            logger.info("chat.executive_start turn_id=%s", turn_id)

            if committee_review:
                stream = executive.stream_chat_with_committee(
                    user_message=message,
                    session=session,
                    retrieved_context=retrieved_context,
                    episodic_context=episodic_context,
                    debug_collector=collector,
                    person_id=caller_person_id,
                    attachment_blocks=attachment_blocks,
                    peer_memory_context=peer_memory_context,
                    briefing_context=briefing_context,
                    page_context_block=page_context_block,
                    turn_id=turn_id,
                    memory_text=memory_text,
                ).__aiter__()
            else:
                stream = executive.stream_chat(
                    user_message=message,
                    session=session,
                    retrieved_context=retrieved_context,
                    episodic_context=episodic_context,
                    debug_collector=collector,
                    person_id=caller_person_id,
                    attachment_blocks=attachment_blocks,
                    peer_memory_context=peer_memory_context,
                    briefing_context=briefing_context,
                    page_context_block=page_context_block,
                    turn_id=turn_id,
                    memory_text=memory_text,
                ).__aiter__()

            # Whole-turn deadline, not per-chunk: a stream that drips bytes
            # forever must still be cut off at timeout_s total.
            deadline = exec_t0 + timeout_s

            # Collect side-effecting action chips as they stream past so they
            # persist with the assistant message (the live UI loses them on
            # reload otherwise) — the same dicts the client renders inline.
            action_chips: list[dict[str, Any]] = []

            async def _persist_turn() -> None:
                # Persist the turn on every terminal path — normal completion,
                # timeout, AND client disconnect — so leaving a chat
                # mid-response no longer drops it. The user message and
                # whatever the Executive produced before the break are saved
                # together, matching the "save what's generated" contract.
                #
                # Only persists when there's a real response: a zero-token
                # disconnect saves nothing, which keeps the stored history a
                # clean user/assistant alternation for the next turn.
                #
                # The outbound `chat_turn` audit row is emitted inside
                # Executive.stream_chat() / stream_chat_with_committee() so
                # every entry point (web stream here, .chat() wrapper used by
                # Discord / Slack / Telegram / Email / Google Chat) records
                # the response uniformly. Don't duplicate it here.
                if not full_response:
                    return
                if not is_first_turn:
                    update_session_timestamp(session.session_id)
                save_message(
                    session.session_id, "user", message, sender_person_id=caller_person_id
                )
                persisted["assistant_message_id"] = save_message(
                    session.session_id,
                    "assistant",
                    full_response,
                    action_chips=json.dumps(action_chips) if action_chips else None,
                    stopped=stopped,
                )
                # The Executive's own post-turn block (executive.py, after its
                # `async for`) is what normally mirrors the turn into the live
                # in-memory Session. On every broken-out path it is skipped,
                # because the generator is closed at its yield — and
                # `_get_or_create_session` never re-reads history for a session
                # already in `_sessions`. Without this the NEXT turn in this
                # process would build its prompt as though the stopped turn had
                # never happened, while a page reload showed it.
                if stopped or client_disconnected or timed_out:
                    session.add_user_message(message)
                    session.add_assistant_message(full_response)
                logger.info(
                    "chat.turn_persisted turn_id=%s is_first_turn=%s disconnected=%s stopped=%s",
                    turn_id, is_first_turn, client_disconnected, stopped,
                )

                # First-turn rename: replace the truncated-message
                # placeholder set by create_session() with a Haiku-generated
                # topic title. Awaited (not fire-and-forget) so the sidebar
                # picks up the good title on the refresh that follows the
                # `done` event we yield below — no second roundtrip needed.
                #
                # Hard timeout on the title call so a slow/hung Haiku can't
                # stall the SSE `done` event indefinitely. On timeout we
                # leave the placeholder title in place.
                if is_first_turn and stopped:
                    # The rename is a Haiku round-trip with a
                    # `utility_fast_timeout_s` budget (10s by default), and on
                    # the stop path the user is waiting on the stream to close
                    # — a stop that takes ten seconds to finish is not a stop.
                    # But it cannot simply be skipped either: the rename only
                    # ever runs on a first turn, and the mirror above has
                    # already made this turn part of the history, so turn 2
                    # would not be a first turn and the session would keep its
                    # truncated-message placeholder title forever. Run it
                    # detached instead, so the stream closes immediately and
                    # the sidebar picks the title up on its next refresh.
                    _rename_session_in_background(
                        session.session_id, message, full_response, turn_id
                    )
                elif is_first_turn:
                    from openexecutive.memory.session_store import (
                        update_session_title,
                    )
                    from openexecutive.utils.session_title import (
                        generate_session_title,
                    )
                    try:
                        new_title = await asyncio.wait_for(
                            generate_session_title(message, full_response),
                            timeout=settings.utility_fast_timeout_s,
                        )
                        if new_title:
                            update_session_title(session.session_id, new_title)
                    except Exception:
                        # asyncio.TimeoutError is a subclass of Exception in
                        # Python 3.11+; catching Exception covers both the
                        # timeout and any other failure (DB write, etc).
                        logger.exception(
                            "chat.title_update_failed turn_id=%s", turn_id
                        )

            # One long-lived waiter, created once. Re-creating it per iteration
            # would race `Event.set()` and could miss a stop that landed
            # between two steps.
            stop_waiter: asyncio.Task[bool] | None = (
                asyncio.ensure_future(stop_event.wait())
                if stop_event is not None
                else None
            )
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        # Catches a stop that landed before this step — including
                        # during the pre-stream context gather, where we have not
                        # asked the Executive for anything yet and the stop
                        # therefore costs nothing.
                        stopped = True
                        logger.info(
                            "chat.stopped turn_id=%s chunks=%d", turn_id, chunk_count
                        )
                        break

                    if await request.is_disconnected():
                        client_disconnected = True
                        logger.info("chat.client_disconnected turn_id=%s", turn_id)
                        break

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        logger.warning(
                            "chat.timeout turn_id=%s timeout_s=%s collected_chunks=%d",
                            turn_id, timeout_s, chunk_count,
                        )
                        break

                    step = await _next_stream_step(
                        stream, stop_waiter, remaining, turn_id
                    )

                    if step.exhausted:
                        break

                    if step.produced:
                        item = step.item
                        if isinstance(item, str):
                            if item == Executive._THINKING:
                                data = json.dumps({
                                    "type": "thinking",
                                    "session_id": session.session_id,
                                })
                            else:
                                full_response += item
                                chunk_count += 1
                                data = json.dumps({
                                    "type": "chunk",
                                    "content": item,
                                    "session_id": session.session_id,
                                })
                            yield f"data: {data}\n\n"
                        else:
                            if isinstance(item, dict) and item.get("type") == "action_taken":
                                action_chips.append(item)
                            yield f"data: {json.dumps(item)}\n\n"

                        if stop_event is not None and stop_event.is_set():
                            stopped = True
                            logger.info(
                                "chat.stopped turn_id=%s chunks=%d", turn_id, chunk_count
                            )
                            break
                        continue

                    if step.stopped:
                        stopped = True
                        logger.info(
                            "chat.stopped turn_id=%s chunks=%d", turn_id, chunk_count
                        )
                    else:
                        timed_out = True
                        logger.warning(
                            "chat.timeout turn_id=%s timeout_s=%s collected_chunks=%d",
                            turn_id, timeout_s, chunk_count,
                        )
                    break
            finally:
                if stop_waiter is not None:
                    # Otherwise: "Task was destroyed but it is pending".
                    stop_waiter.cancel()

            logger.info(
                "chat.executive_done turn_id=%s chunks=%d duration_s=%.2f timed_out=%s disconnected=%s stopped=%s",
                turn_id, chunk_count, time.monotonic() - exec_t0, timed_out,
                client_disconnected, stopped,
            )

            # Best-effort cancel of the underlying iterator if we broke out early.
            # On the stop path the awaited cancellation above has already closed
            # the generator, so this is a no-op there; it stays as the safety net
            # for the other break-outs.
            if timed_out or client_disconnected or stopped:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()

            if client_disconnected:
                # Connection is gone, so we can't yield anything more — but we
                # still persist whatever the Executive produced before the
                # disconnect, so navigating away mid-response doesn't lose the
                # turn (it shows up in the sidebar when the user returns).
                await _persist_turn()
                return

            # Persist BEFORE the terminal frames on every path, including the
            # stop. Yielding first would mean that closing the tab in the gap
            # between the `stopped` frame and this call loses the partial reply
            # — which is the one thing the feature exists to preserve. The
            # first-turn title call, which used to make that gap ~10s wide, is
            # skipped on the stop path above, so persisting first no longer
            # makes the button feel dead.
            await _persist_turn()

            if stopped:
                # Not an `error` event — a stop is a user decision, not a
                # failure.
                yield f"data: {json.dumps({'type': 'stopped', 'session_id': session.session_id})}\n\n"
                # The Executive is cancelled before its own post-turn block runs,
                # so the outbound `chat_turn` row it would have written never
                # happens. Record the stop here instead, or the audit log shows
                # an inbound turn with no response side at all.
                audit_log(
                    "chat_turn",
                    f"Stopped by user after {chunk_count} chunk(s)",
                    session_id=session.session_id,
                    turn_id=turn_id,
                    actor="executive",
                    details={
                        "direction": "out",
                        "stopped": True,
                        "chunks": chunk_count,
                    },
                )

            if timed_out:
                err_evt = collector.emit("turn_error", {"reason": "timeout", "timeout_s": timeout_s})
                yield f"data: {json.dumps(collector.to_sse_dict(err_evt))}\n\n"
                err = json.dumps({
                    "type": "error",
                    "message": f"Response timed out after {timeout_s:.0f}s",
                    "session_id": session.session_id,
                })
                yield f"data: {err}\n\n"

            complete_evt = collector.emit("turn_complete", {
                "chunks": chunk_count,
                "duration_s": round(time.monotonic() - exec_t0, 3),
                "timed_out": timed_out,
                "stopped": stopped,
            })
            yield f"data: {json.dumps(collector.to_sse_dict(complete_evt))}\n\n"

            done_payload: dict[str, Any] = {"type": "done", "session_id": session.session_id}
            if persisted.get("assistant_message_id"):
                done_payload["message_id"] = persisted["assistant_message_id"]
            done = json.dumps(done_payload)
            yield f"data: {done}\n\n"

        except Exception:
            # Don't leak exception text (may contain API keys, paths, etc.) to clients.
            logger.exception("chat.turn_failed turn_id=%s", turn_id)
            error = json.dumps({
                "type": "error",
                "message": "An internal error occurred. Please try again.",
                "session_id": session.session_id,
            })
            yield f"data: {error}\n\n"
            done = json.dumps({"type": "done", "session_id": session.session_id})
            yield f"data: {done}\n\n"
        finally:
            # Snapshot for /debug/last-turn.
            global _last_turn_events, _last_turn_meta
            _last_turn_events = [collector.to_sse_dict(e) for e in collector._events]
            _last_turn_meta = {
                "turn_id": turn_id,
                "session_id": session.session_id,
                "is_first_turn": is_first_turn,
                "chunks": chunk_count,
                "duration_s": round(time.monotonic() - exec_t0, 3),
                "timed_out": timed_out,
                "client_disconnected": client_disconnected,
                "stopped": stopped,
            }
            _release_stop(client_turn_id, turn_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    return await _run_chat_turn(
        message=body.message,
        session_id=body.session_id,
        committee_review=body.committee_review,
        attachment_blocks=None,
        request=request,
        page_context=body.page_context,
        client_turn_id=body.client_turn_id,
        memory_text=body.memory_text,
    )


@router.post("/chat/stop")
async def chat_stop(body: StopChatRequest, request: Request) -> dict[str, str]:
    """Stop an in-flight /chat turn, addressed by its `client_turn_id`.

    404 means "no live turn of yours with that id" — a turn that already
    finished and a turn belonging to someone else are deliberately
    indistinguishable from here, so this can't be used to probe which ids are
    live. Stopping is best-effort by design: the SSE stream stays open and
    remains the authority on how the turn actually ended.
    """
    client_turn_id = _clean_client_turn_id(body.client_turn_id)
    stopped_turn_id = (
        _request_stop(
            client_turn_id,
            _stop_owner_key(request, _resolve_caller_person_id(request)),
        )
        if client_turn_id
        else None
    )
    if stopped_turn_id is None:
        raise HTTPException(status_code=404, detail="No in-flight turn with that id")
    logger.info("chat.stop_requested turn_id=%s", stopped_turn_id)
    return {"status": "stopping", "turn_id": stopped_turn_id}


@router.post("/chat/upload")
async def chat_upload(
    request: Request,
    message: str = Form(..., min_length=1, max_length=32000),
    session_id: str | None = Form(None),
    committee_review: bool = Form(False),
    # Size bound only — see the note on ChatRequest.client_turn_id for why the
    # format is checked by `_clean_client_turn_id` instead of rejected here.
    client_turn_id: str | None = Form(None, max_length=64),
    files: list[UploadFile] = File(...),  # noqa: B008 — FastAPI multipart marker, mirrors the pattern for File parameters
) -> StreamingResponse:
    """Streaming chat turn with file/photo attachments.

    Documents (PDF/DOCX/TXT/MD/CSV) have their text extracted, inlined into
    the user message, and indexed into the ChromaDB ``inbound_attachments``
    collection as a background task — exactly the behavior the Discord and
    Telegram bots already use. That collection is never retrieved from, so an
    attachment informs the turn that carried it and no later one. Images are converted into Anthropic vision
    blocks and passed through ``attachment_blocks``.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > _MAX_FILES_PER_TURN:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files: limit {_MAX_FILES_PER_TURN} per turn",
        )

    text_parts: list[str] = []
    image_blocks: list[dict[str, Any]] = []
    filenames: list[str] = []

    for upload in files:
        filename = upload.filename or "attachment"
        filenames.append(filename)
        data = await upload.read()
        if len(data) > _MAX_BYTES_PER_FILE:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{filename}: file too large — "
                    f"{len(data) // (1024 * 1024)} MB "
                    f"(limit {_MAX_BYTES_PER_FILE // (1024 * 1024)} MB)"
                ),
            )

        try:
            extra_text, blocks = build_attachment_output(
                filename, data, upload.content_type or "",
            )
        except Exception:
            logger.exception("chat_upload: processing failed for %s", filename)
            text_parts.append(f"(Could not process {filename})")
            continue

        if extra_text:
            text_parts.append(extra_text)
        image_blocks.extend(blocks)

    if text_parts:
        attachments_block = "\n\n".join(text_parts)
        augmented_message = f"{message}\n\n{attachments_block}"
    else:
        augmented_message = message

    return await _run_chat_turn(
        message=augmented_message,
        session_id=session_id,
        committee_review=committee_review,
        attachment_blocks=image_blocks or None,
        request=request,
        client_turn_id=client_turn_id,
        # The extracted document text is the document's words, not the
        # caller's: recorded in peer memory it becomes facts about the caller.
        memory_text=f"{message}\n\n[Attached: {', '.join(filenames)}]",
    )


@router.get("/debug/last-turn")
def get_last_turn() -> dict[str, Any]:
    """Returns the most recently completed turn's debug events.

    Single-process only — for local debugging when SSE itself failed.
    """
    return {"meta": _last_turn_meta, "events": _last_turn_events}


# ---------------------------------------------------------------------------
# Suggested starter prompts for the chat empty-state.
# ---------------------------------------------------------------------------

_FALLBACK_PROMPTS: list[str] = [
    "Where did we land on this quarter's priorities?",
    "Pull the team in on a decision I'm sitting on.",
    "Let's review the board update before it goes out.",
    "What's changed since our last sync?",
]

# Shown when the LLM can't be reached or no company context exists. Mirrors
# the historical static subtitle that lived in the UI before this change.
_FALLBACK_SUBTITLE: str = (
    "Pick up where we left off — decisions to revisit, drafts to push "
    "forward, people to pull in."
)

# In-memory TTL cache. Single-process FastAPI deployments; bumped on every
# profile/session change via the cache key.
_SUGGESTED_PROMPTS_TTL_S = 600
_suggested_prompts_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _build_prompts_context(
    profile: Any, recent_titles: list[str], caller: Any = None
) -> tuple[str, str]:
    """Return (context_quality, user_content) for the utility-fast call.

    `context_quality` is one of "rich" | "thin" | "empty" — surfaced in the
    response so the client (and future analytics) can tell how grounded the
    suggestions are.

    `caller` is the resolved Person reading these prompts (or None). Naming
    them lets the model avoid suggesting the reader loop *themselves* in —
    nonsensical, and the canonical "pull in Jordan" bug when Jordan is the one
    looking at the cards. It does not affect `context_quality`.
    """
    has_profile = profile is not None and not profile.is_empty()
    quality = "rich" if has_profile and recent_titles else (
        "thin" if has_profile or recent_titles else "empty"
    )

    parts: list[str] = []
    if caller is not None:
        who = f"{caller.full_name} ({caller.role})" if caller.role else caller.full_name
        principal_tag = " — the principal / company owner" if caller.is_principal else ""
        parts.append(f"CURRENT USER (the person reading these prompts): {who}{principal_tag}.")
        parts.append("")
    if has_profile:
        parts.append("Company profile:")
        parts.append(profile.to_prompt_block())
    if recent_titles:
        parts.append("")
        parts.append("Recent chat topics (most recent first):")
        for t in recent_titles[:5]:
            parts.append(f"- {t}")
    if not parts:
        parts.append("(No company profile or chat history available.)")
    return quality, "\n".join(parts)


_PROMPTS_SYSTEM = (
    "You write the empty-state for a virtual executive's chat landing "
    "screen, grounded in the supplied company context.\n\n"
    "WHO THE EXECUTIVE IS: a peer on the user's executive team — a "
    "colleague who shares ownership of company outcomes, coordinates "
    "with the rest of the team across channels, watches what is "
    "happening on its own, and brings small calls to the user only "
    "when they need to decide. The executive helps the team think, "
    "decide, draft, prioritize, prepare for meetings, and "
    "pressure-test plans. The executive does NOT build, ship, sell, "
    "or operate the company's product — the company does that. A "
    "line like 'your executive can ship AI' or 'your executive can "
    "automate workflows' is WRONG, because the company ships those "
    "things, not the executive. The executive's job is to help the "
    "team RUN the company.\n\n"
    "Output ONLY a JSON object with exactly two keys — no prose, no "
    "markdown, no fences:\n"
    '  "subtitle": one sentence, 8-16 words, that opens a specific '
    "conversation the user is likely to want today. Pick ONE concrete "
    "priority, decision, metric, person, or recent topic from the "
    "context and lean into it — never a list of capabilities or "
    "disciplines, never a description of what the executive 'can do'. "
    "Read like a thoughtful colleague picking up a thread, not a "
    "marketing tagline or a service-desk greeting. Banned words/"
    "phrases: 'leverage', 'unlock', 'actually works', 'ship', "
    "'automate workflows', 'integrate your tools', 'how can I help', "
    "any three-item comma list. No greeting. No exclamation marks. "
    "End with a period or question mark.\n"
    '  "prompts": an array of exactly 4 starter phrases, each 4-12 '
    "words, written in the voice of a colleague picking up a "
    "thread — decisions to revisit, people to pull in, work to push "
    "forward, sync-style check-ins. Mix question and imperative "
    "forms (e.g. 'Where did we land on the Q3 plan?', 'Pull "
    "marketing in on this.', \"Let's tighten the hiring slate.\"). "
    "AVOID the canned-FAQ shape ('What should our top priorities "
    "be?', 'How do we think about pricing?'). Ground in the company "
    "context — name the actual priority, person, metric, or recent "
    "topic when the context supplies one. If recent chat topics are "
    "provided, lean toward fresh angles that build on them rather "
    "than repeating them verbatim.\n\n"
    "NEVER write a prompt (or subtitle) that suggests looping in, "
    "pulling in, DMing, messaging, following up with, or notifying the "
    "reader themselves. The context names the reader under 'CURRENT "
    "USER' — that person is who these prompts are FOR, so 'Loop "
    "<reader> in' or 'Pull <reader> into this' is nonsensical. You may "
    "suggest pulling in *other* named people or departments; never the "
    "reader."
)


def _cache_key(
    profile: Any,
    latest_session_id: str | None,
    caller_person_id: int | None,
) -> str:
    import hashlib
    import json as _json

    if profile is None or profile.is_empty():
        payload = ""
    else:
        payload = _json.dumps(
            {
                "name": profile.name,
                "industry": profile.industry,
                "stage": profile.stage,
                "priorities": profile.strategic_priorities.current_year,
                "north_star": profile.strategic_priorities.north_star_metric,
                "pain_points": profile.target_customer.pain_points,
                "competitors": profile.competitive_landscape.primary_competitors[:3],
            },
            sort_keys=True,
        )
    # Include caller in the key — recent_titles passed to the LLM are
    # user-scoped, so two users with the same profile must not share a
    # cached payload.
    raw = f"{payload}|{latest_session_id or ''}|{caller_person_id or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


async def _fast_json_call(
    system: str, user_content: str, max_tokens: int, log_tag: str
) -> dict[str, Any] | None:
    """One utility-fast call that must answer with a JSON object.

    Returns the parsed object, or None on any failure (timeout, API error,
    malformed or non-object JSON) — callers fall back rather than raise.
    """
    from openexecutive.agents.utility_fast import get_fast_model
    from openexecutive.config import get_settings
    from openexecutive.providers import get_provider

    try:
        model = get_fast_model()
        response = await asyncio.wait_for(
            get_provider(model).messages_create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            ),
            timeout=get_settings().utility_fast_timeout_s,
        )
        text_blocks = [b for b in response.content if getattr(b, "type", "") == "text"]
        raw = text_blocks[0].text.strip() if text_blocks else ""
        if raw.startswith("```"):
            # Strip ```json fences if the model added them anyway. Use
            # removeprefix (not lstrip) — lstrip("json") would also strip
            # legitimate leading j/s/o/n characters from the JSON body.
            parts = raw.split("```")
            if len(parts) >= 2:
                raw = parts[1].removeprefix("json").strip()
        data = json.loads(raw)
    except Exception:
        logger.exception("%s: LLM call failed", log_tag)
        return None
    return data if isinstance(data, dict) else None


async def _generate_prompts_via_llm(
    user_content: str,
) -> tuple[list[str], str] | None:
    """Returns (prompts, subtitle) on success, None on any failure.

    Both fields must validate: prompts >= 4 non-empty strings, subtitle a
    non-empty string. Any short-fall returns None so the caller can fall
    back rather than render a half-broken empty state.
    """
    data = await _fast_json_call(_PROMPTS_SYSTEM, user_content, 500, "suggested_prompts")
    if data is None:
        return None
    prompts_raw = data.get("prompts")
    subtitle_raw = data.get("subtitle")
    if not isinstance(prompts_raw, list) or not isinstance(subtitle_raw, str):
        return None
    prompts = [str(p).strip() for p in prompts_raw if isinstance(p, str) and p.strip()]
    subtitle = subtitle_raw.strip()
    if len(prompts) < 4 or not subtitle:
        return None
    return prompts[:4], subtitle


# ---------------------------------------------------------------------------
# Suggested follow-up for the chat composer, shown after each reply.
# ---------------------------------------------------------------------------

_FOLLOWUP_SYSTEM = (
    "You suggest the user's next message in a conversation with their "
    "virtual executive — a peer on their executive team who helps them "
    "think, decide, draft, prioritize, and pull the right people in.\n\n"
    "Read the transcript and write the ONE message the user would most "
    "naturally send next, in the user's own voice (first person, "
    "addressed to the executive). Build on the executive's last reply: "
    "take the obvious next step it opens up — go deeper on one point, "
    "act on a recommendation, get a draft, pressure-test a claim, or "
    "decide between options it laid out. Name the concrete thing "
    "(the person, metric, draft, or decision) rather than saying 'this' "
    "or 'that'. Never repeat a question the user already asked. Never "
    "suggest looping in, messaging, or following up with the user "
    "themselves.\n\n"
    "Output ONLY a JSON object with exactly one key — no prose, no "
    "markdown, no fences:\n"
    '  "suggestion": one sentence, 4-14 words, no greeting, no '
    "exclamation marks, ending with a period or question mark."
)

# Per-message budget when building the transcript. The follow-up only
# needs the thread's gist, and the reply being followed up on can be long.
_FOLLOWUP_MAX_MESSAGES = 6
_FOLLOWUP_MAX_CHARS_PER_MESSAGE = 1500
_FOLLOWUP_MAX_WORDS = 24


def _build_followup_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages[-_FOLLOWUP_MAX_MESSAGES:]:
        speaker = "EXECUTIVE" if m.get("role") == "assistant" else "USER"
        text = str(m.get("content") or "").strip()
        if len(text) > _FOLLOWUP_MAX_CHARS_PER_MESSAGE:
            text = text[:_FOLLOWUP_MAX_CHARS_PER_MESSAGE] + " […]"
        lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines)


async def _generate_followup_via_llm(transcript: str) -> str | None:
    """Returns one suggested next user message, or None on any failure."""
    data = await _fast_json_call(_FOLLOWUP_SYSTEM, transcript, 150, "followup_suggestion")
    if data is None:
        return None
    raw = data.get("suggestion")
    if not isinstance(raw, str):
        return None
    suggestion = " ".join(raw.split())
    # A runaway or empty answer is worse than no suggestion in the composer.
    if not suggestion or len(suggestion.split()) > _FOLLOWUP_MAX_WORDS:
        return None
    return suggestion


@router.get("/chat/suggested-prompts")
async def get_suggested_prompts(request: Request) -> dict[str, Any]:
    """Return 4 starter prompts + a contextual subtitle for the chat
    empty-state.

    Generated via the utility-fast model from the company profile + recent
    session titles (scoped to the calling user, so each user sees prompts
    personalized to their own history). Cached in-process for ~10 minutes
    per (profile, latest_session_id) pair. Always returns a usable 4-item
    list and a non-empty subtitle; falls back to a static set on any failure.
    """
    from openexecutive.memory.session_store import list_sessions
    from openexecutive.onboarding.profile_builder import load_or_create_profile

    try:
        profile = load_or_create_profile()
    except Exception:
        logger.exception("suggested_prompts: profile load failed")
        profile = None

    caller_person_id = _resolve_caller_person_id(request)
    caller = None
    if caller_person_id is not None:
        try:
            from openexecutive.people.store import get_person
            caller = get_person(caller_person_id)
        except Exception:
            logger.warning("suggested_prompts: caller lookup failed", exc_info=True)
    try:
        sessions = list_sessions(caller_person_id) if caller_person_id is not None else []
    except Exception:
        logger.exception("suggested_prompts: session list failed")
        sessions = []

    recent_titles = [s["title"] for s in sessions[:5] if s.get("title")]
    latest_session_id = sessions[0]["session_id"] if sessions else None

    key = _cache_key(profile, latest_session_id, caller_person_id)
    now = time.monotonic()
    hit = _suggested_prompts_cache.get(key)
    if hit is not None and (now - hit[0]) < _SUGGESTED_PROMPTS_TTL_S:
        return hit[1]

    quality, user_content = _build_prompts_context(profile, recent_titles, caller)

    if quality == "empty":
        # Deterministic fallback for a fresh install — safe to cache.
        payload = {
            "prompts": list(_FALLBACK_PROMPTS),
            "subtitle": _FALLBACK_SUBTITLE,
            "context_quality": "empty",
        }
        _suggested_prompts_cache[key] = (now, payload)
        return payload

    generated = await _generate_prompts_via_llm(user_content)
    if generated is None:
        # LLM failure (timeout, malformed JSON, transient API error). Return
        # the static set but DO NOT cache — a 10-minute stale fallback after
        # a one-off blip would mask recovery on the next request.
        return {
            "prompts": list(_FALLBACK_PROMPTS),
            "subtitle": _FALLBACK_SUBTITLE,
            "context_quality": "empty",
        }

    prompts, subtitle = generated
    payload = {
        "prompts": prompts,
        "subtitle": subtitle,
        "context_quality": quality,
    }
    _suggested_prompts_cache[key] = (now, payload)
    return payload
