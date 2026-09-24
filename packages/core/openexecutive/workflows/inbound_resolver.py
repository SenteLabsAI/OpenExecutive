"""Inbound message resolver for WaitForHuman workflow pauses.

When a human replies on Slack, Discord or Telegram, this module tries to
match it to an ``awaiting_human`` workflow run. (Email and Google Chat have
no call site — their adapters do not resolve gates.)

Three-tier matching (highest priority first)
-------------------------------------------
1. **Explicit reference** — ``in_reply_to`` matches ``outbound_message_id``
   stored in the run's ``state_json``.  Most reliable; zero LLM cost.

2. **Single-candidate** — the person has exactly ONE ``awaiting_human`` run
   addressed to them on this channel, within the last 7 days.  Reliable
   when the human's workflow queue has exactly one open item.

3. **LLM disambiguation** — multiple candidates exist.  A single Haiku call
   picks the best match and returns a confidence score.  Below
   ``_CONFIDENCE_THRESHOLD`` (0.85) → return ``None`` (caller escalates).

Returns ``WaitForHumanResolution`` on a confident match, ``None`` otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import openexecutive.workflows.persistence as _wf_persistence
from openexecutive.workflows.wait_for_human import (
    _CONFIDENCE_THRESHOLD,
    PARSE_FAILED_KEY,
    WaitForHumanResolution,
    normalize_channel,
    parse_decision,
)

logger = logging.getLogger(__name__)

_RECENCY_DAYS = 7


def _load_awaiting_runs(
    from_person_id: int | None,
    db_path: Path | None,
) -> list[dict]:
    """Return awaiting_human runs for this person, from the last _RECENCY_DAYS days."""
    all_runs = _wf_persistence.list_awaiting_runs(db_path=db_path)
    if not all_runs:
        return []

    cutoff = (datetime.now(UTC) - timedelta(days=_RECENCY_DAYS)).isoformat()
    result = []
    for run in all_runs:
        if from_person_id is not None and run.get("awaiting_person_id") != from_person_id:
            continue
        if run.get("updated_at", "") < cutoff:
            continue
        result.append(run)
    return result


def _parse_state(run: dict) -> dict:
    """The gate's stored state, or `{}` when it cannot be read.

    An unreadable state is NOT the same as an empty one — see
    `_state_is_readable`. Tier 2's legacy wildcard keys off the absence of a
    `delivery` key, and a corrupt row would otherwise present as a legacy row
    and match any message from that person on any channel.
    """
    raw = run.get("state_json") or "{}"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _state_is_readable(run: dict) -> bool:
    """False when `state_json` is present but not a readable JSON object."""
    raw = run.get("state_json")
    if raw is None or raw == "":
        return True  # nothing stored is a legitimate legacy shape
    try:
        return isinstance(json.loads(raw), dict)
    except (json.JSONDecodeError, TypeError):
        return False


def _scoped_to_session(candidates: list[dict], session_ids: Sequence[str]) -> list[dict]:
    """Drop gates that belong to a different conversation.

    A gate with no ``origin_session_id`` (web or scheduler originated) stays in
    play for any session — there is no conversation to tie it to. A gate that
    DOES name a session only matches one of ``session_ids``.

    Callers pass every id this conversation is reachable under, not just one.
    A Slack mention in a channel is answered inside the thread the bot's reply
    roots, which computes a DIFFERENT id from the one the gate recorded — so a
    single-id comparison made that gate permanently unanswerable, recreating
    the exact #136 failure one surface over. The adapter that double-writes
    history across two session ids must offer both here too.
    """
    accepted = {sid for sid in session_ids if sid}
    out = []
    for run in candidates:
        origin = str(_parse_state(run).get("origin_session_id") or "")
        if origin and origin not in accepted:
            continue
        out.append(run)
    return out


async def _make_resolution(
    *,
    run: dict,
    text: str,
    channel: str,
    message_id: str,
    person_id: int,
    expected_shape: str,
    question: str = "",
) -> WaitForHumanResolution | None:
    """Parse the reply into a decision, or return None if it isn't one.

    Returning None lets the caller fall through to the normal chat path. That
    matters most for ``approve_reject``: with an open gate, every message from
    that person on this channel reaches here, and answering an unrelated
    question with "your response has been recorded" would be worse than the
    gate never resolving at all.
    """
    parsed = await parse_decision(text, expected_shape, question=question)
    # The parser's fallback is a fabricated answer, not a verdict — an empty
    # string for free_text, a null for numeric, a defer for approve_reject.
    # Recording any of them would close a sign-off on a parser outage, so the
    # guard applies to EVERY shape, not just approve_reject.
    if parsed.pop(PARSE_FAILED_KEY, False):
        logger.warning(
            "resolver: could not parse a %s reply for run %s — leaving the "
            "run open",
            expected_shape,
            run.get("run_id"),
        )
        return None
    if str(parsed.get("decision") or "") == "unrelated":
        logger.info(
            "resolver: reply for run %s is not an answer to the gate — "
            "leaving the run open",
            run.get("run_id"),
        )
        return None
    return WaitForHumanResolution(
        run_id=run["run_id"],
        reply_text=text,
        source_channel=channel,
        source_message_id=message_id,
        parsed_decision=parsed,
        person_id=person_id,
        resolved_at=datetime.now(UTC).isoformat(),
    )


async def resolve_inbound_message(
    *,
    channel: str,
    channel_ref: str,
    from_person_id: int | None,
    text: str,
    message_id: str = "",
    in_reply_to: str = "",
    session_ids: Sequence[str] = (),
    db_path: Path | None = None,
) -> WaitForHumanResolution | None:
    """Try to match an inbound message to an awaiting_human workflow run.

    Returns a ``WaitForHumanResolution`` on a confident match, ``None``
    if unmatched or confidence below threshold.

    ``session_ids`` are every conversation key this message arrives under —
    usually one, but an adapter whose reply can root a new thread must pass
    both that thread's id and the parent conversation's. A gate raised inside
    a chat session (the person launched the workflow and is the approver)
    records that session and matches ONLY replies from it; otherwise one open
    gate would swallow every message that person sends on the channel, in any
    thread, and answer each with "your response has been recorded".
    """
    if not text.strip():
        return None

    candidates = _load_awaiting_runs(from_person_id, db_path)
    if not candidates:
        return None

    candidates = _scoped_to_session(candidates, session_ids)
    if not candidates:
        return None

    # ------------------------------------------------------------------ #
    # Tier 1: explicit in_reply_to match
    # If the caller supplied an explicit reference and it doesn't match any
    # open run, return None immediately — do NOT fall through to fuzzier
    # tiers with a stale reference (that would mis-match an unrelated run).
    # ------------------------------------------------------------------ #
    if in_reply_to:
        for run in candidates:
            state = _parse_state(run)
            stored_msg_id = state.get("outbound_message_id", "")
            if stored_msg_id and stored_msg_id == in_reply_to:
                logger.info(
                    "resolver: tier-1 match run_id=%s via in_reply_to=%s",
                    run["run_id"], in_reply_to,
                )
                return await _make_resolution(
                    run=run,
                    text=text,
                    channel=channel,
                    message_id=message_id,
                    person_id=from_person_id or run.get("awaiting_person_id") or 0,
                    expected_shape=state.get("expected_reply_shape", "approve_reject"),
                    question=str(state.get("question") or ""),
                )
        # An explicit reference that matched nothing rules out the candidates
        # it COULD have matched — the ones carrying a stored outbound id — and
        # says nothing about the rest. Per-candidate, not a global `any()`: a
        # person holding one delivered gate and one self-approval gate would
        # otherwise have the self gate blocked by the other's outbound id.
        # (Slack also passes its thread_ts on every threaded message, so
        # rejecting outright here made tiers 2 and 3 unreachable — #136.)
        remaining = [
            r for r in candidates if not _parse_state(r).get("outbound_message_id")
        ]
        if not remaining:
            logger.debug(
                "resolver: in_reply_to=%r did not match any run that has an "
                "outbound_message_id — refusing to guess",
                in_reply_to,
            )
            return None
        logger.debug(
            "resolver: in_reply_to=%r matched nothing; falling through to "
            "tier 2 with the %d candidate(s) that carry no outbound id",
            in_reply_to,
            len(remaining),
        )
        candidates = remaining

    # ------------------------------------------------------------------ #
    # Tier 2: single-candidate — exactly one open run on this channel
    # ------------------------------------------------------------------ #
    # Normalize both sides (`slack_dm` and `slack` are the same channel), and
    # treat an empty stored channel as a wildcard: rows checkpointed before
    # gate delivery started populating the field would otherwise be
    # permanently unmatchable.
    wanted = normalize_channel(channel)
    channel_matches = []
    for run in candidates:
        state = _parse_state(run)
        stored = normalize_channel(str(state.get("channel") or ""))
        if stored:
            if stored != wanted:
                continue
            # Address-level narrowing when both sides know it: a gate
            # delivered to this person's Slack DM should not be answerable
            # from a Slack channel. Only applied when the stored ref is
            # non-empty, so a self-approval gate with no ref still matches.
            stored_ref = str(state.get("channel_ref") or "")
            if stored_ref and channel_ref and stored_ref != channel_ref:
                logger.debug(
                    "resolver: run %s was delivered to %s, not %s — skipping",
                    run.get("run_id"), stored_ref, channel_ref,
                )
                continue
            channel_matches.append(run)
            continue
        # No stored channel. Two very different situations share that shape,
        # and `delivery` tells them apart: a checkpoint written before gate
        # delivery existed has no `delivery` key at all and is matched
        # loosely so it stays answerable, while one whose delivery was
        # suppressed / alerted / failed has the key and must NOT be — nobody
        # was asked on any channel, so any message matching it would record
        # an answer to a question that was never put.
        if not _state_is_readable(run):
            logger.warning(
                "resolver: run %s has unreadable state_json — not matchable",
                run.get("run_id"),
            )
        elif "delivery" not in state:
            logger.info(
                "resolver: run %s predates gate delivery (no channel, no "
                "delivery status) — treating as a wildcard",
                run.get("run_id"),
            )
            channel_matches.append(run)
        else:
            logger.debug(
                "resolver: run %s was never delivered (delivery=%s) — not "
                "matchable until it is",
                run.get("run_id"),
                state.get("delivery"),
            )

    if len(channel_matches) == 1:
        run = channel_matches[0]
        state = _parse_state(run)
        logger.info(
            "resolver: tier-2 match run_id=%s (single candidate on channel=%s)",
            run["run_id"], channel,
        )
        return await _make_resolution(
            run=run,
            text=text,
            channel=channel,
            message_id=message_id,
            person_id=from_person_id or run.get("awaiting_person_id") or 0,
            expected_shape=state.get("expected_reply_shape", "approve_reject"),
                    question=str(state.get("question") or ""),
        )

    if len(channel_matches) == 0:
        logger.debug("resolver: no awaiting runs for channel=%s person=%s", channel, from_person_id)
        return None

    # ------------------------------------------------------------------ #
    # Tier 3: LLM disambiguation
    # ------------------------------------------------------------------ #
    logger.info(
        "resolver: tier-3 LLM disambiguation — %d candidates for person=%s",
        len(channel_matches), from_person_id,
    )
    chosen_run_id, confidence = await _llm_disambiguate(text, channel_matches)
    if chosen_run_id is None or confidence < _CONFIDENCE_THRESHOLD:
        logger.info(
            "resolver: LLM confidence %.2f below threshold %.2f — returning None",
            confidence, _CONFIDENCE_THRESHOLD,
        )
        return None

    matched = next((r for r in channel_matches if r["run_id"] == chosen_run_id), None)
    if matched is None:
        return None

    state = _parse_state(matched)
    logger.info(
        "resolver: tier-3 match run_id=%s confidence=%.2f", chosen_run_id, confidence
    )
    return await _make_resolution(
        run=matched,
        text=text,
        channel=channel,
        message_id=message_id,
        person_id=from_person_id or matched.get("awaiting_person_id") or 0,
        expected_shape=state.get("expected_reply_shape", "approve_reject"),
                    question=str(state.get("question") or ""),
    )


async def resolve_and_acknowledge(
    *,
    channel: str,
    channel_ref: str,
    person_id: int,
    text: str,
    send: Callable[[str], Awaitable[None]],
    message_id: str = "",
    in_reply_to: str = "",
    session_ids: Sequence[str] = (),
) -> bool:
    """Answer an open wait-for-human gate with this inbound message, if it is one.

    Returns True when a gate was resolved and the acknowledgement sent — the
    caller must stop processing the message. False means it was not an answer
    and should continue down the normal chat path.

    Every inbound adapter needs exactly this sequence, and each one used to
    carry its own copy: resolve, apply, acknowledge, return. The copies had
    already drifted (Slack passed a thread id as `in_reply_to` where Telegram
    passed ""), and a new argument like `session_id` had three places to be
    forgotten. One helper, one `send` callback per channel.

    Never raises: an inbound message must still reach the chat path if the
    resolver or the run store is having a bad day.
    """
    from openexecutive.workflows.resumer import (
        apply_resolution,
        resolution_acknowledgement,
    )

    try:
        resolution = await resolve_inbound_message(
            channel=channel,
            channel_ref=channel_ref,
            from_person_id=person_id,
            text=text,
            message_id=message_id,
            in_reply_to=in_reply_to,
            session_ids=session_ids,
        )
        if resolution is None or not resolution.run_id:
            return False
        if not await apply_resolution(resolution.run_id, resolution):
            # Already resolved or timed out — not ours to answer.
            return False
    except Exception:
        logger.exception(
            "resolver: inbound check failed for channel=%s person=%s",
            channel,
            person_id,
        )
        return False

    # The gate is CLOSED from here on. A failed acknowledgement send must not
    # report the message as unhandled — the caller would fall it through to
    # alert triage and a full chat turn, so the person's sign-off would be
    # recorded while they got an unrelated reply. That is the swallowed-
    # approval shape #136 was filed for.
    try:
        await send(
            await asyncio.to_thread(
                resolution_acknowledgement, resolution.run_id, resolution
            )
        )
    except Exception:
        logger.exception(
            "resolver: resolved run %s but could not acknowledge it to "
            "person %s on %s",
            resolution.run_id,
            person_id,
            channel,
        )
    return True




async def _llm_disambiguate(
    text: str,
    candidates: list[dict],
) -> tuple[str | None, float]:
    """Ask Haiku to pick the best-matching run from multiple candidates.

    Returns (run_id, confidence). On failure returns (None, 0.0).
    """
    import json as _json

    candidate_summaries = "\n".join(
        f"[{i + 1}] run_id={r['run_id']} workflow={r.get('workflow_name','')} "
        f"title={r.get('title','')} "
        f"question={_parse_state(r).get('question','')[:120]}"
        for i, r in enumerate(candidates[:6])
    )

    prompt = (
        "A human sent this reply:\n"
        f"{text[:500]}\n\n"
        "Which of these open workflow pauses is it most likely responding to?\n\n"
        f"{candidate_summaries}\n\n"
        'Return JSON: {"run_id": "<chosen run_id>", "confidence": <0.0-1.0>}\n'
        "confidence = how certain you are this reply is for that run. "
        "If none fit well, set confidence below 0.85."
    )

    try:
        import asyncio

        from openexecutive.agents.utility_fast import get_fast_model
        from openexecutive.config import get_settings
        from openexecutive.providers import get_provider

        model = get_fast_model()
        response = await asyncio.wait_for(
            get_provider(model).messages_create(
                model=model,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=get_settings().utility_fast_timeout_s,
        )
        raw_text_blocks = [b for b in response.content if getattr(b, "type", "") == "text"]
        raw = raw_text_blocks[0].text.strip() if raw_text_blocks else ""
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        data = _json.loads(raw)
        return data.get("run_id"), float(data.get("confidence", 0.0))
    except Exception:
        logger.exception("resolver: LLM disambiguation failed")
        return None, 0.0
