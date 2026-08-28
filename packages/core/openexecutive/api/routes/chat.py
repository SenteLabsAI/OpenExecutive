from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from openexecutive.api.models import ChatRequest, PageContext
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


def _get_or_create_session(session_id: str | None, request: Request) -> Any:
    from openexecutive.memory.session_store import load_messages
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.session import Session

    if session_id and session_id in _sessions:
        return _sessions[session_id]

    new_id = session_id or str(uuid.uuid4())
    profile = load_or_create_profile()
    session = Session(session_id=new_id, company_profile=profile if not profile.is_empty() else None)

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


def _caller_can_manage_roster(request: Request) -> bool:
    """Return true only for a principal authenticated by the trusted UI proxy."""
    caller_email = (request.headers.get("x-caller-email") or "").strip().lower()
    if not caller_email:
        return False
    try:
        from openexecutive.api.authorization import require_principal

        require_principal(caller_email)
    except (HTTPException, OSError, sqlite3.Error):
        return False
    return True


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
            from openexecutive.api.authorization import configured_principal_email

            principal = find_principal_person()
            configured_principal = configured_principal_email()
            if (
                principal is not None
                and configured_principal
                and caller_email == configured_principal
            ):
                return principal.id
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
) -> StreamingResponse:
    """Shared streaming-chat handler for both the JSON and multipart routes.

    `message` is the (already attachment-augmented) text the Executive will
    see and that we persist as the user turn. `attachment_blocks` carries
    image content blocks; document text is expected to already be inlined in
    `message` by the caller.
    """
    from openexecutive.config import get_settings
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.memory.session_store import create_session
    from openexecutive.orchestrator.executive import Executive

    t0 = time.monotonic()
    turn_id = uuid.uuid4().hex
    collector = DebugCollector(t0=t0, turn_id=turn_id)

    session = _get_or_create_session(session_id, request)
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
        full={"message": message},
    )

    # Resolve the caller. Used for Honcho's per-person memory AND for
    # tagging the session row's owner (so /sessions can filter the
    # sidebar by signed-in user). See `_resolve_caller_person_id` for
    # the precedence rule that protects against cross-identity leakage.
    caller_person_id = _resolve_caller_person_id(request)
    can_manage_roster = _caller_can_manage_roster(request)

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

    async def _do_episodic() -> str:
        try:
            return await asyncio.to_thread(format_for_prompt)
        except Exception:
            logger.exception("chat.format_for_prompt_failed turn_id=%s", turn_id)
            return ""

    async def _do_briefing() -> str:
        # Current open-alert digest + open-search digest so the Executive can
        # discuss a briefing item the principal clicked or named (the items
        # behind the /today "What's going on" narrative) AND the live executive
        # searches. Sync SQLite reads off the event loop; both formatters
        # swallow their own errors, so this is belt-and-suspenders.
        from openexecutive.briefing.context import format_open_alerts_for_prompt
        from openexecutive.briefing.onboarding_digest import format_onboarding_for_prompt
        from openexecutive.briefing.talent_digest import format_talent_for_prompt

        # return_exceptions=True so one digest raising never discards the others —
        # each formatter already swallows its own errors, this just guards the
        # to_thread wrappers themselves.
        results = await asyncio.gather(
            asyncio.to_thread(format_open_alerts_for_prompt),
            asyncio.to_thread(format_talent_for_prompt),
            asyncio.to_thread(format_onboarding_for_prompt),
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
                    reasoning_level="medium" if committee_review else "low",
                )
            except Exception:
                # The wrapper itself catches and returns "" on timeout/error,
                # so this is belt-and-suspenders — but if a future change to
                # the helper raises, don't sink the whole gather() with it.
                logger.exception("chat.honcho_prefetch_failed turn_id=%s", turn_id)
                return ""

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

    executive = Executive(mcp_gateway=getattr(request.app.state, "mcp_gateway", None))
    settings = get_settings()
    timeout_s = settings.chat_stream_timeout_s

    if committee_review:
        # Committee adds reviewer fan-out + a full revision pass on top of
        # the draft. Extend the whole-turn deadline so the route doesn't
        # cut us off mid-revision.
        timeout_s += settings.committee_extra_timeout_s

    async def event_generator():
        from openexecutive.memory.session_store import (
            save_message,
            update_session_timestamp,
        )

        full_response = ""
        chunk_count = 0
        exec_t0 = time.monotonic()
        timed_out = False
        client_disconnected = False

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
                    can_manage_roster=can_manage_roster,
                    attachment_blocks=attachment_blocks,
                    peer_memory_context=peer_memory_context,
                    briefing_context=briefing_context,
                    page_context_block=page_context_block,
                ).__aiter__()
            else:
                stream = executive.stream_chat(
                    user_message=message,
                    session=session,
                    retrieved_context=retrieved_context,
                    episodic_context=episodic_context,
                    debug_collector=collector,
                    person_id=caller_person_id,
                    can_manage_roster=can_manage_roster,
                    attachment_blocks=attachment_blocks,
                    peer_memory_context=peer_memory_context,
                    briefing_context=briefing_context,
                    page_context_block=page_context_block,
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
                save_message(session.session_id, "user", message)
                save_message(
                    session.session_id,
                    "assistant",
                    full_response,
                    action_chips=json.dumps(action_chips) if action_chips else None,
                )
                logger.info(
                    "chat.turn_persisted turn_id=%s is_first_turn=%s disconnected=%s",
                    turn_id, is_first_turn, client_disconnected,
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
                if is_first_turn:
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

            while True:
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

                try:
                    item = await asyncio.wait_for(stream.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    timed_out = True
                    logger.warning(
                        "chat.timeout turn_id=%s timeout_s=%s collected_chunks=%d",
                        turn_id, timeout_s, chunk_count,
                    )
                    break

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

            logger.info(
                "chat.executive_done turn_id=%s chunks=%d duration_s=%.2f timed_out=%s disconnected=%s",
                turn_id, chunk_count, time.monotonic() - exec_t0, timed_out, client_disconnected,
            )

            # Best-effort cancel of the underlying iterator if we broke out early.
            if timed_out or client_disconnected:
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

            await _persist_turn()

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
            })
            yield f"data: {json.dumps(collector.to_sse_dict(complete_evt))}\n\n"

            done = json.dumps({"type": "done", "session_id": session.session_id})
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
            }

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
    )


@router.post("/chat/upload")
async def chat_upload(
    request: Request,
    message: str = Form(..., min_length=1, max_length=32000),
    session_id: str | None = Form(None),
    committee_review: bool = Form(False),
    files: list[UploadFile] = File(...),  # noqa: B008 — FastAPI multipart marker, mirrors the pattern for File parameters
) -> StreamingResponse:
    """Streaming chat turn with file/photo attachments.

    Documents (PDF/DOCX/TXT/MD/CSV) have their text extracted, inlined into
    the user message, and indexed into the ChromaDB ``company_docs``
    collection as a background task — exactly the behavior the Discord and
    Telegram bots already use. Images are converted into Anthropic vision
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

    for upload in files:
        filename = upload.filename or "attachment"
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


async def _generate_prompts_via_llm(
    user_content: str,
) -> tuple[list[str], str] | None:
    """Returns (prompts, subtitle) on success, None on any failure.

    Both fields must validate: prompts >= 4 non-empty strings, subtitle a
    non-empty string. Any short-fall returns None so the caller can fall
    back rather than render a half-broken empty state.
    """
    import asyncio
    import json as _json

    from openexecutive.agents.utility_fast import get_fast_model
    from openexecutive.config import get_settings
    from openexecutive.providers import get_provider

    try:
        model = get_fast_model()
        response = await asyncio.wait_for(
            get_provider(model).messages_create(
                model=model,
                max_tokens=500,
                system=_PROMPTS_SYSTEM,
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
        data = _json.loads(raw)
        if not isinstance(data, dict):
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
    except Exception:
        logger.exception("suggested_prompts: LLM call failed")
        return None


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
