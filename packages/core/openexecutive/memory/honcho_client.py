"""Thin async wrapper around the self-hosted Honcho memory service.

Honcho gives us per-person long-term memory keyed off ``Person.id`` so the
Executive can recall facts about a user across Slack, Discord, Telegram,
email, and the web UI in one peer card. This module exposes three
surfaces the orchestrator calls per turn:

- :func:`prefetch` — asks Honcho a dialectic question about the inbound
  user and returns a short ``<peer_memory>`` block that the Executive
  injects into the user turn (never the system block — see
  ``CLAUDE.md`` / prompt caching rules). Bounded by
  ``Settings.honcho_prefetch_timeout_s`` so a Honcho outage can't stall
  the chat turn. Accepts ``reasoning_level`` to trade latency for
  synthesis depth (``"low"`` for the per-turn path, bumped to
  ``"medium"``/``"high"`` for committee-reviewed turns).
- :func:`sync_turn` — fire-and-forget persist of the completed exchange
  so Honcho's server-side extraction can update the peer card. Accepts
  ``co_present_person_ids`` to add all distinct humans in a thread
  (Discord, Slack channel, email cc) as peers in the same Honcho
  session — the data shape that makes peer-of-peer reasoning possible.
- :func:`directional_chat` — backs the ``ask_about_person`` Anthropic
  tool. Queries one peer's representation of another via Honcho's
  ``target=`` parameter (e.g. "what does Alice know about Bob"),
  returning the synthesized answer.

Both prefetch and sync_turn are no-ops when ``Settings.honcho_enabled``
is false or ``person_id`` is ``None`` (anonymous channel user with no
Person row). All Honcho calls are wrapped so failures degrade OE to its
pre-Honcho behaviour rather than break a turn. Every call writes one
``peer_memory`` audit row so the per-turn flow chart can render the
Honcho path alongside ``<retrieved_context>`` and ``<past_decisions>``.

Cache discipline: the returned block is short, query-shaped, and goes in
the user turn — it does NOT live in any cached system block.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Literal

from openexecutive.audit import log_event as audit_log
from openexecutive.audit.context import get_active_ids, set_turn
from openexecutive.config import get_settings

logger = logging.getLogger(__name__)

# Stable peer id for OE's Executive in Honcho's peer model. Using a single
# fixed id (rather than per-deployment) keeps the assistant's representation
# consistent if a workspace is shared across environments.
_EXECUTIVE_PEER_ID = "executive"

# Prefix that turns a department slug into a Honcho peer id. Keeping it in
# a constant (rather than f-stringing inline) means a future rename only
# touches one place AND makes the namespace boundary easy to grep for
# when auditing the wire format.
_DEPARTMENT_PEER_PREFIX = "department_"


def _department_peer_id(department_slug: str) -> str:
    """Return the Honcho peer id for a department slug.

    Slugs like ``marketing-and-sales`` already conform to Honcho's
    ``^[a-zA-Z0-9_-]+$`` charset, but we still funnel through
    ``_safe_honcho_id`` so a future slug source (CSV import, manual edit)
    can't 422 the wrapper. The prefix uses an underscore (not a colon)
    so the result stays in-charset even before sanitization.
    """
    return _safe_honcho_id(f"{_DEPARTMENT_PEER_PREFIX}{department_slug}")

# Honcho's API rejects session/peer ids that don't match `^[a-zA-Z0-9_-]+$`
# with a 422 UnprocessableEntityError. Adapter session ids like
# `discord:thread:1508292084245725194` or `telegram:8519677317` contain
# colons and get rejected wholesale — every sync_turn fails before any
# data lands. Replace any disallowed char with `_` before sending.
_HONCHO_ID_BAD = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_honcho_id(raw: str) -> str:
    return _HONCHO_ID_BAD.sub("_", raw)

# Honcho's accepted reasoning_level values, surfaced as a type alias so the
# Executive entry points can annotate the kwarg they thread through.
ReasoningLevel = Literal["minimal", "low", "medium", "high", "max"]

# Clients are cached **per event loop**, not globally. The Slack adapter
# uses `asyncio.run(...)` once per inbound message, which spins up a fresh
# event loop each turn — a globally cached Honcho client would hold an
# httpx AsyncClient bound to the *first* loop, then silently fail on
# every subsequent Slack turn ("Event loop is closed").
#
# We key by the running-loop *object* (loops are hashable). `id(loop)`
# would be wrong: CPython readily reuses memory addresses across
# back-to-back `asyncio.run()` calls because the previous loop is GC'd
# before the next one is created. Holding the loop object keeps it
# alive, so we sweep entries whose loop has been closed on every
# cache-miss path — bounded growth without an explicit teardown hook.
_clients: dict[asyncio.AbstractEventLoop, Any] = {}
_client_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

# Fire-and-forget tasks need a strong reference or the event loop only
# holds them weakly — CPython is then free to GC the task mid-flight,
# losing the sync and emitting "Task was destroyed but it is pending"
# warnings. The done-callback removes the entry once the task finishes.
_pending_sync_tasks: set[asyncio.Task[None]] = set()


def _current_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running event loop, or None outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _sweep_closed_loops() -> None:
    """Drop cache entries whose event loop has been closed.

    Cheap (O(N) in cached loops; usually 1) and keeps the dict from
    growing unboundedly as Slack's per-turn `asyncio.run` accumulates
    short-lived loops over the bot's uptime.
    """
    stale = [loop for loop in _clients if loop.is_closed()]
    for loop in stale:
        _clients.pop(loop, None)
        _client_locks.pop(loop, None)


def _get_lock(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    lock = _client_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _client_locks[loop] = lock
    return lock


async def _get_client() -> Any | None:
    """Return a configured Honcho client for the current event loop.

    Honcho's constructor itself doesn't make network calls, but the first
    ``.peer()`` / ``.session()`` triggers a workspace-ensure POST. We let
    that happen lazily and catch it at the call site rather than here.
    """
    settings = get_settings()
    if not settings.honcho_enabled:
        return None
    loop = _current_loop()
    if loop is None:
        return None
    if loop in _clients:
        return _clients[loop]
    _sweep_closed_loops()
    async with _get_lock(loop):
        if loop in _clients:
            return _clients[loop]
        try:
            from honcho import Honcho

            client = Honcho(
                api_key=settings.honcho_api_key,
                base_url=settings.honcho_base_url,
                # Use the active workspace (env default OR per-fixture
                # override set by cli/fixture_loader.py). See
                # `get_active_workspace_id` above.
                workspace_id=get_active_workspace_id(),
                timeout=settings.honcho_prefetch_timeout_s,
            )
            _clients[loop] = client
            return client
        except Exception:
            logger.exception("honcho: client construction failed; disabling for this process")
            return None


def _drop_cached_clients() -> None:
    """Drop every cached client + lock + pending task across loops."""
    _clients.clear()
    _client_locks.clear()
    _pending_sync_tasks.clear()


def reset_client_for_tests() -> None:
    """Drop all cached clients + locks + pending tasks so test fixtures can rebuild."""
    _drop_cached_clients()


# --------------------------------------------------------------------------- #
# Active workspace override (used to isolate demo-fixture traffic from the
# operator's real Honcho data — see cli/fixture_loader.py).
#
# When a fixture is loaded, the loader calls ``set_active_workspace_id`` with
# a per-fixture id like ``openexec-fixture-halcyon_motors-a1b2c3d4`` so all demo
# turns sync to a sacrificial workspace. On unload, the loader deletes the
# demo workspace and clears the override; the env-default workspace
# (``settings.honcho_workspace_id``) takes over again.
#
# State is persisted to a small JSON file under the snapshot backup dir
# so it survives process restarts. Reading the file on every Honcho call
# is cheap (one stat + sub-millisecond JSON parse on cache miss).
# --------------------------------------------------------------------------- #


def _active_workspace_state_path():  # type: ignore[no-untyped-def]
    from pathlib import Path

    settings = get_settings()
    profile_path = Path(settings.company_profile_path)
    return profile_path.parent / "_user_backup" / ".honcho_active_workspace.json"


def get_active_workspace_id() -> str:
    """Return the workspace_id Honcho should use right now.

    Reads the persisted override (set by ``set_active_workspace_id``)
    when present; falls back to ``settings.honcho_workspace_id``. Any
    parse error or missing file silently falls back too — a corrupt
    override must never break Honcho calls.
    """
    settings = get_settings()
    try:
        path = _active_workspace_state_path()
        if not path.exists():
            return settings.honcho_workspace_id
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        wid = data.get("workspace_id")
        if isinstance(wid, str) and wid:
            return wid
    except Exception:
        logger.warning("honcho: active-workspace state read failed; using env default")
    return settings.honcho_workspace_id


def set_active_workspace_id(workspace_id: str, *, fixture_name: str | None = None) -> None:
    """Persist a workspace_id override and drop cached clients.

    Subsequent ``_get_client()`` calls will construct against the new
    workspace_id. The previous env-default value is preserved implicitly
    by ``settings.honcho_workspace_id`` (we never modify env).
    """
    import json
    from datetime import UTC, datetime

    path = _active_workspace_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workspace_id": workspace_id,
        "fixture_name": fixture_name,
        "set_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _drop_cached_clients()


def clear_active_workspace_id() -> None:
    """Remove the override; the env-default workspace takes over."""
    path = _active_workspace_state_path()
    path.unlink(missing_ok=True)
    _drop_cached_clients()


_WORKSPACE_DELETE_TIMEOUT_S = 5.0
_SESSION_DELETE_TIMEOUT_S = 5.0
# Hard ceiling on the whole purge phase so a workspace with thousands of
# broken sessions can't keep teardown busy indefinitely. If we hit this
# the audit row records `purge_truncated=true` and we still attempt the
# workspace delete (it will likely ConflictError, but the audit makes
# the cause visible).
_SESSION_PURGE_BUDGET_S = 30.0
# How many session-delete coroutines to keep in flight at once. Honcho
# tolerates parallel deletes fine; the cap is to avoid socket exhaustion
# when a demo fixture racked up hundreds of chat sessions.
_SESSION_PURGE_CONCURRENCY = 8


async def _purge_workspace_sessions(client: Any, workspace_id: str) -> dict[str, Any]:
    """Delete every session in ``workspace_id`` using ``client``.

    Honcho's ``delete_workspace`` refuses with ``ConflictError`` while
    sessions remain — the CLI exposes this as ``--cascade``, but the
    Python SDK (2.x) has no equivalent flag, so we sweep manually.

    ``client`` must already be bound to ``workspace_id`` (sessions are
    workspace-scoped). Best-effort: per-session failures are counted
    and reported in the returned dict but do not raise.
    """
    sem = asyncio.Semaphore(_SESSION_PURGE_CONCURRENCY)

    async def _del_one(session: Any) -> tuple[bool, str | None]:
        async with sem:
            try:
                await asyncio.wait_for(
                    session.aio.delete(),
                    timeout=_SESSION_DELETE_TIMEOUT_S,
                )
                return True, None
            except Exception as exc:
                return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    # ``client.aio.sessions()`` is ``async def`` and returns an
    # ``AsyncPage[Session]`` once awaited; the page itself is async
    # iterable and transparently pages through results. Awaiting must
    # happen FIRST — iterating the coroutine directly would TypeError.
    sessions: list[Any] = []
    list_error: str | None = None
    try:
        page = await client.aio.sessions()
        async for session in page:
            sessions.append(session)
    except Exception as exc:
        # If we can't even list sessions the workspace is already gone or
        # auth is broken — record it and let the outer delete_workspace
        # attempt surface the real error.
        list_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    summary: dict[str, Any] = {"deleted": 0, "failed": 0, "errors": []}
    if list_error is not None:
        summary["list_error"] = list_error
        return summary

    if not sessions:
        return summary

    try:
        # Overall budget on the entire purge. Results from in-flight
        # coroutines that completed before the timeout fired are lost,
        # but the audit row marks the truncation so an operator can see
        # it and rerun the reset.
        results = await asyncio.wait_for(
            asyncio.gather(*(_del_one(s) for s in sessions), return_exceptions=False),
            timeout=_SESSION_PURGE_BUDGET_S,
        )
    except TimeoutError:
        summary["purge_truncated"] = True
        summary["budget_s"] = _SESSION_PURGE_BUDGET_S
        return summary

    # Aggregate after gather so dict updates aren't interleaved between
    # concurrent tasks (cooperative scheduling makes this safe in
    # practice on CPython, but accumulating sequentially is plainly safe).
    for ok, err in results:
        if ok:
            summary["deleted"] += 1
        else:
            summary["failed"] += 1
            if err is not None and len(summary["errors"]) < 5:
                summary["errors"].append(err)
    return summary


def _build_teardown_client(settings: Any, workspace_id: str) -> Any | None:
    """Construct a one-shot Honcho client bound to ``workspace_id``.

    Workspace-scoped operations (``sessions()`` and the cascading session
    deletes) only see the workspace the client was constructed against.
    The module-level cached client follows the active-workspace override
    file, which may point elsewhere during reset — so for teardown we
    build an explicit client and throw it away.
    """
    try:
        from honcho import Honcho

        return Honcho(
            api_key=settings.honcho_api_key,
            base_url=settings.honcho_base_url,
            workspace_id=workspace_id,
            timeout=settings.honcho_prefetch_timeout_s,
        )
    except Exception:
        logger.exception(
            "honcho: teardown client construction failed for workspace_id=%s",
            workspace_id,
        )
        return None


async def delete_workspace_and_reset_client(workspace_id: str | None = None) -> None:
    """Permanently wipe a Honcho workspace and drop cached clients.

    Called by ``reset_all_state`` (default ``workspace_id`` → current
    active workspace) and by the fixture loader's unload path (explicit
    ``workspace_id`` of the demo fixture being torn down).

    Honcho's SDK auto-creates the workspace on next peer access against
    whatever workspace_id is active, so no recreate step is needed.

    **Cascade behavior**: Honcho rejects ``delete_workspace`` with
    ``ConflictError`` while any session in the workspace remains. The
    SDK has no native cascade flag (only the CLI does), so we explicitly
    drain sessions first via ``_purge_workspace_sessions``. The teardown
    client is constructed *bound to the target workspace* so the session
    listing hits the right scope regardless of the active-workspace
    override file's contents (which may point elsewhere mid-reset).

    Failures are swallowed + audited. A Honcho outage (or a key without
    delete permission) must never block the local cleanup from completing.

    Note on the race window: in-flight chat turns that already captured
    a local ``client`` reference will continue using it after the cache
    is dropped. Reset/unload is operator-initiated and rare, so the
    window is tolerable — a follow-up could gate chat traffic behind a
    reset epoch if this becomes a real issue.
    """
    t0 = time.monotonic()
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(op="workspace_reset", person_id=None, outcome="disabled")
        return

    target = workspace_id or get_active_workspace_id()
    if not target:
        # Defensive: ``honcho_workspace_id`` is required when
        # ``honcho_enabled`` is true, so this is near-unreachable. But a
        # malformed override file could leave ``get_active_workspace_id``
        # returning the empty string, and the SDK would then pick its
        # own default — silently deleting the wrong workspace.
        _emit_peer_memory(
            op="workspace_reset",
            person_id=None,
            outcome="error",
            details={"reason": "no_target_workspace"},
        )
        return

    # Build a teardown-only client explicitly bound to ``target``. The
    # cascade and the workspace delete must hit the *target* workspace,
    # not whatever the active-workspace override currently says — using
    # the cached client here would, in unload_fixture's just-cleared
    # state, list sessions from the env-default workspace and then
    # delete the right workspace, guaranteeing ConflictError (the bug
    # this PR exists to fix). On construction failure (ImportError,
    # missing settings), audit ``no_client`` and bail.
    client = _build_teardown_client(settings, target)
    if client is None:
        _emit_peer_memory(
            op="workspace_reset",
            person_id=None,
            outcome="error",
            details={"reason": "no_client"},
        )
        return

    # Step 1: drain sessions. Bounded by its own per-session timeout, so
    # a misbehaving Honcho can't stall reset indefinitely.
    purge = await _purge_workspace_sessions(client, target)
    purge_details: dict[str, Any] = {
        "sessions_deleted": purge["deleted"],
        "sessions_failed": purge["failed"],
    }
    if purge["errors"]:
        purge_details["sessions_purge_errors"] = purge["errors"]
    if "list_error" in purge:
        purge_details["sessions_list_error"] = purge["list_error"]
    if purge.get("purge_truncated"):
        purge_details["purge_truncated"] = True
        purge_details["purge_budget_s"] = purge["budget_s"]

    # Step 2: delete the (now-empty) workspace.
    try:
        await asyncio.wait_for(
            client.aio.delete_workspace(target),
            timeout=_WORKSPACE_DELETE_TIMEOUT_S,
        )
        _emit_peer_memory(
            op="workspace_reset",
            person_id=None,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"workspace_id": target, **purge_details},
        )
    except TimeoutError:
        logger.warning(
            "honcho: delete_workspace timed out for workspace_id=%s",
            target,
        )
        _emit_peer_memory(
            op="workspace_reset",
            person_id=None,
            outcome="timeout",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"workspace_id": target, **purge_details},
        )
    except Exception as exc:
        logger.exception(
            "honcho: delete_workspace failed for workspace_id=%s",
            target,
        )
        _emit_peer_memory(
            op="workspace_reset",
            person_id=None,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "workspace_id": target,
                **purge_details,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )
    finally:
        # Whatever happened on the server, drop our cached client so the
        # next call rebuilds — the SDK auto-creates the workspace on
        # first peer access against the post-delete state.
        _drop_cached_clients()


def _emit_peer_memory(
    *,
    op: str,
    person_id: int | None,
    outcome: str,
    department_slug: str | None = None,
    duration_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one `peer_memory` audit row.

    Single funnel so the audit-row shape stays consistent across every
    call site (prefetch, sync_turn, directional_chat, and the
    department-keyed analogs). ``op`` distinguishes the call type;
    ``outcome`` is one of
    ``ok|timeout|error|disabled|no_person|no_department|no_loop|empty``.
    Department-keyed ops pass ``department_slug``; person-only ops leave
    it None. session_id / turn_id come from the orchestrator's
    ``set_turn`` ContextVar so the row groups with the rest of the turn
    in the flow chart.
    """
    payload: dict[str, Any] = {
        "op": op,
        "person_id": person_id,
        "outcome": outcome,
    }
    if department_slug is not None:
        payload["department_slug"] = department_slug
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if details:
        payload.update(details)
    dept_part = f" dept={department_slug}" if department_slug is not None else ""
    # log_event swallows its own exceptions; we're already in a path
    # where Honcho call paths must never break a turn, so even a
    # double-swallow here is the right posture.
    audit_log(
        "peer_memory",
        f"honcho.{op}: person_id={person_id}{dept_part} outcome={outcome}",
        actor="honcho",
        details=payload,
    )


async def prefetch(
    query: str,
    *,
    person_id: int | None,
    session_id: str | None = None,
    reasoning_level: ReasoningLevel = "low",
) -> str:
    """Return a short ``<peer_memory>`` block for ``person_id``, or ``""``.

    Sent through Honcho's dialectic chat endpoint with the inbound user
    message as the query, so the synthesized answer is targeted at what
    the Executive actually needs to know right now — not a fixed
    most-recent slice.

    ``reasoning_level`` trades latency for synthesis depth. The standard
    per-turn path uses ``"low"`` (fast, fits the 3s prefetch budget);
    committee-reviewed turns and explicit deep-research workflows can
    bump to ``"medium"``/``"high"`` for a richer answer at the cost of
    a few extra seconds and a Sonnet-tier OpenRouter call.

    Times out at ``Settings.honcho_prefetch_timeout_s``; on any failure
    we return an empty string so the turn still runs with whatever the
    builtin episodic block provides. Every outcome (ok/timeout/error/
    disabled/no_person) emits one `peer_memory` audit row.
    """
    t0 = time.monotonic()
    if person_id is None:
        _emit_peer_memory(op="prefetch", person_id=None, outcome="no_person")
        return ""
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(op="prefetch", person_id=person_id, outcome="disabled")
        return ""
    client = await _get_client()
    if client is None:
        # `disabled` already-flagged above; this branch covers the
        # construction-failed case (logged by `_get_client`).
        _emit_peer_memory(op="prefetch", person_id=person_id, outcome="error")
        return ""
    try:
        answer = await asyncio.wait_for(
            _do_prefetch(client, query, person_id, reasoning_level),
            timeout=settings.honcho_prefetch_timeout_s,
        )
        _emit_peer_memory(
            op="prefetch",
            person_id=person_id,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "query_preview": query[:160],
                "response_chars": len(answer),
                "reasoning_level": reasoning_level,
            },
        )
        return answer
    except TimeoutError:
        logger.info("honcho: prefetch timed out for person_id=%s", person_id)
        _emit_peer_memory(
            op="prefetch",
            person_id=person_id,
            outcome="timeout",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"reasoning_level": reasoning_level},
        )
        return ""
    except Exception:
        logger.exception("honcho: prefetch failed for person_id=%s", person_id)
        _emit_peer_memory(
            op="prefetch",
            person_id=person_id,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"reasoning_level": reasoning_level},
        )
        return ""


async def _do_prefetch(
    client: Any,
    query: str,
    person_id: int,
    reasoning_level: ReasoningLevel,
) -> str:
    peer = await client.aio.peer(str(person_id))
    # Intentionally do NOT pass `session=`. The cross-channel pain we are
    # addressing requires the *global* peer representation — Alice on Slack
    # should surface in a later Discord turn. Scoping to a single Honcho
    # session would re-silo by channel.
    answer = await peer.aio.chat(
        query,
        reasoning_level=reasoning_level,
    )
    # `chat()` returns `str | None`; treat None / whitespace as "nothing to
    # add" so callers can do a simple truthiness check.
    return (answer or "").strip()


async def _do_directional(
    client: Any,
    person_id: int,
    question: str,
    target_person_id: int | None,
    reasoning_level: ReasoningLevel,
) -> str | None:
    """The async body of :func:`directional_chat`, kept separate so
    :func:`asyncio.wait_for` can budget the whole thing including the
    peer-resolution HTTP calls (not just the chat call)."""
    peer = await client.aio.peer(str(person_id))
    target = (
        await client.aio.peer(str(target_person_id))
        if target_person_id is not None
        else None
    )
    return await peer.aio.chat(
        question,
        target=target,
        reasoning_level=reasoning_level,
    )


async def directional_chat(
    person_id: int,
    question: str,
    *,
    target_person_id: int | None = None,
    reasoning_level: ReasoningLevel = "medium",
) -> str:
    """Query peer ``person_id``'s representation, optionally directed at another peer.

    Backs the ``ask_about_person`` Anthropic tool. Two modes:

    - ``target_person_id=None`` — ask peer A's global representation
      ("what does Alice prefer?").
    - ``target_person_id=B`` — ask peer A's representation **of peer B**
      ("what has Alice said about Bob?"). This is the peer-of-peer
      directional query that makes knowledge-graph-shaped reasoning
      possible.

    Returns the synthesized answer or ``""`` if Honcho is disabled / the
    call fails. Defaults to ``reasoning_level="medium"`` because tool
    calls are typically deliberate deep-dives, not the per-turn budget
    path — the model already paid the latency cost by deciding to call.

    Bounded at 30s. Longer than prefetch's 3s budget because the model
    deliberately invoked this tool, but still a ceiling — without one a
    Honcho hang would pin the entire tool-call loop until
    CHAT_STREAM_TIMEOUT_S fires (~2 min), starving every other tool
    call in the same turn.
    """
    t0 = time.monotonic()
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(op="directional_chat", person_id=person_id, outcome="disabled")
        return ""
    client = await _get_client()
    if client is None:
        _emit_peer_memory(op="directional_chat", person_id=person_id, outcome="error")
        return ""
    try:
        answer = await asyncio.wait_for(
            _do_directional(client, person_id, question, target_person_id, reasoning_level),
            timeout=30.0,
        )
        text = (answer or "").strip()
        _emit_peer_memory(
            op="directional_chat",
            person_id=person_id,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "target_person_id": target_person_id,
                "question_preview": question[:160],
                "response_chars": len(text),
                "reasoning_level": reasoning_level,
            },
        )
        return text
    except TimeoutError:
        logger.info(
            "honcho: directional_chat timed out person_id=%s target=%s",
            person_id,
            target_person_id,
        )
        _emit_peer_memory(
            op="directional_chat",
            person_id=person_id,
            outcome="timeout",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "target_person_id": target_person_id,
                "reasoning_level": reasoning_level,
            },
        )
        return ""
    except Exception:
        logger.exception(
            "honcho: directional_chat failed person_id=%s target=%s",
            person_id,
            target_person_id,
        )
        _emit_peer_memory(
            op="directional_chat",
            person_id=person_id,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"target_person_id": target_person_id},
        )
        return ""


def sync_turn(
    user_message: str,
    assistant_response: str,
    *,
    person_id: int | None,
    session_id: str | None,
    co_present_person_ids: list[int] | None = None,
) -> None:
    """Fire-and-forget persist of a completed exchange to Honcho.

    ``co_present_person_ids`` lists every distinct human in the
    conversation context besides the inbound sender (Discord thread
    participants, Slack channel speakers, email cc'd people). They get
    added to the Honcho session as peers so subsequent peer-of-peer
    queries (`directional_chat(target=...)`) have data to reason over.
    Pass ``None`` or ``[]`` for 1:1 conversations.

    Scheduled on the current event loop as a background task so it
    never blocks the user-facing response. Silent on failure — Honcho's
    own retention is best-effort and we don't want a sync error to
    surface after the user already has their answer.
    """
    if person_id is None:
        _emit_peer_memory(op="sync_turn", person_id=None, outcome="no_person")
        return
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(op="sync_turn", person_id=person_id, outcome="disabled")
        return
    if not user_message.strip() and not assistant_response.strip():
        _emit_peer_memory(op="sync_turn", person_id=person_id, outcome="empty")
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called from a sync context with no loop — nothing to schedule.
        # Surface as a distinct outcome so an unexpected sync caller
        # is visible in the audit log rather than silently dropped.
        _emit_peer_memory(op="sync_turn", person_id=person_id, outcome="no_loop")
        return
    # Snapshot the co-present list — caller may mutate it after we return.
    co_present_snapshot = list(co_present_person_ids or [])
    # Snapshot the audit ContextVars at scheduling time. By the time the
    # background task runs, the parent's ``with set_turn(...)`` block has
    # exited and the ContextVars are back to None — so without this
    # snapshot, every fire-and-forget audit row lands with
    # ``session_id=NULL`` and is invisible in the per-session audit view.
    audit_sid, audit_tid = get_active_ids()
    task = loop.create_task(
        _do_sync(
            user_message,
            assistant_response,
            person_id,
            session_id,
            co_present_snapshot,
            audit_sid,
            audit_tid,
        )
    )
    # Retain a strong reference so CPython doesn't GC the task before it
    # finishes (`loop.create_task` only registers a weak ref). The
    # discard callback frees the slot once the task settles, so the set
    # stays bounded by in-flight syncs.
    _pending_sync_tasks.add(task)
    task.add_done_callback(_pending_sync_tasks.discard)


async def _do_sync(
    user_message: str,
    assistant_response: str,
    person_id: int,
    session_id: str | None,
    co_present_person_ids: list[int],
    audit_session_id: str | None,
    audit_turn_id: str | None,
) -> None:
    # Re-bind the audit ContextVars from the snapshot taken at scheduling
    # time so ``_emit_peer_memory`` (which reads them via the audit logger
    # funnel) tags the row with the right turn. Without this, the parent's
    # ``with set_turn(...)`` block in stream_chat() has already exited by
    # the time we run, leaving session_id/turn_id as None on every row we
    # emit — invisible in the per-session audit view.
    with set_turn(session_id=audit_session_id, turn_id=audit_turn_id):
        await _do_sync_body(
            user_message, assistant_response, person_id, session_id,
            co_present_person_ids,
        )


async def _do_sync_body(
    user_message: str,
    assistant_response: str,
    person_id: int,
    session_id: str | None,
    co_present_person_ids: list[int],
) -> None:
    t0 = time.monotonic()
    client = await _get_client()
    if client is None:
        _emit_peer_memory(op="sync_turn", person_id=person_id, outcome="error")
        return
    try:
        user_peer = await client.aio.peer(str(person_id))
        exec_peer = await client.aio.peer(_EXECUTIVE_PEER_ID)
        sess = await client.aio.session(
            _safe_honcho_id(session_id or f"person-{person_id}")
        )
        # Resolve co-present peers + add them to the session so directional
        # queries can later ask "what does <co-present> know about
        # <sender>" and vice versa. Skip the sender (already a peer via
        # user_peer) and dedupe.
        co_present_unique = sorted(
            {pid for pid in co_present_person_ids if pid != person_id}
        )
        if co_present_unique:
            extra_peers = [
                await client.aio.peer(str(pid)) for pid in co_present_unique
            ]
            await sess.aio.add_peers(extra_peers)
        msgs = []
        if user_message.strip():
            msgs.append(user_peer.message(user_message))
        if assistant_response.strip():
            msgs.append(exec_peer.message(assistant_response))
        if msgs:
            await sess.aio.add_messages(msgs)
        _emit_peer_memory(
            op="sync_turn",
            person_id=person_id,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "peer_count": 2 + len(co_present_unique),
                "co_present_person_ids": co_present_unique,
                "message_count": len(msgs),
                "session_id": session_id,
            },
        )
    except Exception as exc:
        logger.exception(
            "honcho: sync_turn failed for person_id=%s session_id=%s",
            person_id,
            session_id,
        )
        _emit_peer_memory(
            op="sync_turn",
            person_id=person_id,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "session_id": session_id,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )


# --------------------------------------------------------------------------- #
# Department-keyed surfaces
#
# Same wire pattern as the person-keyed surfaces above, but the "owner"
# peer is the department itself (`department_<slug>`) instead of a
# Person.id. Two reasons for the parallel-but-separate API rather than
# overloading the existing functions with a `department_slug=` kwarg:
#
# 1. The disabled/no-owner short-circuits are different — `no_person`
#    vs `no_department` make audit-grepping for "which scope skipped"
#    cleanly separable.
# 2. Department turns naturally accumulate a *different* co-presence
#    shape (other dept slugs join the session, not just people), so the
#    write-path branches anyway. Keeping the functions split keeps each
#    one short enough to read end-to-end.
# --------------------------------------------------------------------------- #


async def prefetch_department(
    query: str,
    *,
    department_slug: str | None,
    session_id: str | None = None,
    reasoning_level: ReasoningLevel = "low",
) -> str:
    """Return a short ``<department_memory>`` block for ``department_slug``.

    Mirrors :func:`prefetch` but queries the department's Honcho peer
    instead of a person peer. Used inside specialist routing: when a
    dept-bound specialist is consulted we ask "what does this
    department's institutional memory have to say about <query>", and
    inject the answer alongside the specialist's RAG context.

    Same timeout, audit, and disabled-gate semantics as :func:`prefetch`.
    Returns ``""`` on any failure so the specialist still runs with
    whatever its other context blocks provide.
    """
    t0 = time.monotonic()
    if not department_slug:
        _emit_peer_memory(
            op="prefetch_department",
            person_id=None,
            outcome="no_department",
        )
        return ""
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(
            op="prefetch_department",
            person_id=None,
            department_slug=department_slug,
            outcome="disabled",
        )
        return ""
    client = await _get_client()
    if client is None:
        _emit_peer_memory(
            op="prefetch_department",
            person_id=None,
            department_slug=department_slug,
            outcome="error",
        )
        return ""
    try:
        answer = await asyncio.wait_for(
            _do_prefetch_department(client, query, department_slug, reasoning_level),
            timeout=settings.honcho_prefetch_timeout_s,
        )
        _emit_peer_memory(
            op="prefetch_department",
            person_id=None,
            department_slug=department_slug,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "query_preview": query[:160],
                "response_chars": len(answer),
                "reasoning_level": reasoning_level,
            },
        )
        return answer
    except TimeoutError:
        logger.info(
            "honcho: prefetch_department timed out for department_slug=%s",
            department_slug,
        )
        _emit_peer_memory(
            op="prefetch_department",
            person_id=None,
            department_slug=department_slug,
            outcome="timeout",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"reasoning_level": reasoning_level},
        )
        return ""
    except Exception:
        logger.exception(
            "honcho: prefetch_department failed for department_slug=%s",
            department_slug,
        )
        _emit_peer_memory(
            op="prefetch_department",
            person_id=None,
            department_slug=department_slug,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"reasoning_level": reasoning_level},
        )
        return ""


async def _do_prefetch_department(
    client: Any,
    query: str,
    department_slug: str,
    reasoning_level: ReasoningLevel,
) -> str:
    peer = await client.aio.peer(_department_peer_id(department_slug))
    # No session= argument — same rationale as person prefetch: we want
    # the department peer's *global* representation, not a slice scoped
    # to one conversation thread.
    answer = await peer.aio.chat(query, reasoning_level=reasoning_level)
    return (answer or "").strip()


def sync_department_turn(
    user_message: str,
    assistant_response: str,
    *,
    department_slug: str | None,
    session_id: str | None,
    originating_person_id: int | None = None,
    co_present_person_ids: list[int] | None = None,
    co_present_department_slugs: list[str] | None = None,
) -> None:
    """Fire-and-forget persist of a dept-scoped exchange to Honcho.

    Called once per turn for each department whose specialist contributed
    (the Executive collects this set during routing). Writes into a
    department-scoped Honcho session (``dept-<slug>-<session>``) so the
    dept peer accumulates a coherent timeline distinct from the
    person-scoped session populated by :func:`sync_turn`.

    ``originating_person_id`` is the human who sent the inbound message —
    they author ``user_message`` so the dept peer's representation
    extraction sees user words attributed to *the user*, not to the dept.
    Without this distinction, future ``peer.chat()`` on the dept peer
    would paraphrase user questions back as if the dept itself held those
    views, which is wrong. The originating person joins the session as a
    co-present peer alongside the dept peer (which authors no message
    here — its representation derives from session participation).

    ``co_present_person_ids`` and ``co_present_department_slugs`` carry
    *additional* humans / departments that touched the turn (other
    specialists, thread participants). They become co-present peers too,
    giving Honcho the peer-graph cross-pollination the design called for.

    Silent on failure for the same reason as :func:`sync_turn`: Honcho's
    own retention is best-effort and a sync error must never surface
    after the user already has their answer.
    """
    if not department_slug:
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=None,
            outcome="no_department",
        )
        return
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=None,
            department_slug=department_slug,
            outcome="disabled",
        )
        return
    if not user_message.strip() and not assistant_response.strip():
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=None,
            department_slug=department_slug,
            outcome="empty",
        )
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=None,
            department_slug=department_slug,
            outcome="no_loop",
        )
        return
    # Dedupe co-present people against the originator — they're already
    # going to be a peer via author attribution, so listing them again in
    # co_present is a no-op at best and inflates audit details at worst.
    co_present_people = sorted(
        {
            pid
            for pid in (co_present_person_ids or [])
            if pid != originating_person_id
        }
    )
    co_present_depts = sorted(
        {slug for slug in (co_present_department_slugs or []) if slug != department_slug}
    )
    # Snapshot the audit ContextVars at scheduling time — see _do_sync
    # for the rationale (the parent's set_turn block has already exited
    # by the time this background task runs).
    audit_sid, audit_tid = get_active_ids()
    task = loop.create_task(
        _do_sync_department(
            user_message,
            assistant_response,
            department_slug,
            session_id,
            originating_person_id,
            co_present_people,
            co_present_depts,
            audit_sid,
            audit_tid,
        )
    )
    _pending_sync_tasks.add(task)
    task.add_done_callback(_pending_sync_tasks.discard)


async def _do_sync_department(
    user_message: str,
    assistant_response: str,
    department_slug: str,
    session_id: str | None,
    originating_person_id: int | None,
    co_present_person_ids: list[int],
    co_present_department_slugs: list[str],
    audit_session_id: str | None,
    audit_turn_id: str | None,
) -> None:
    # Re-bind the audit ContextVars so peer_memory rows we emit land
    # on the right session in the per-turn flow chart.
    with set_turn(session_id=audit_session_id, turn_id=audit_turn_id):
        await _do_sync_department_body(
            user_message, assistant_response, department_slug, session_id,
            originating_person_id, co_present_person_ids,
            co_present_department_slugs,
        )


async def _do_sync_department_body(
    user_message: str,
    assistant_response: str,
    department_slug: str,
    session_id: str | None,
    originating_person_id: int | None,
    co_present_person_ids: list[int],
    co_present_department_slugs: list[str],
) -> None:
    t0 = time.monotonic()
    client = await _get_client()
    if client is None:
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=originating_person_id,
            department_slug=department_slug,
            outcome="error",
        )
        return
    try:
        dept_peer = await client.aio.peer(_department_peer_id(department_slug))
        exec_peer = await client.aio.peer(_EXECUTIVE_PEER_ID)
        # Dept session id keeps dept and person syncs from colliding in
        # the same Honcho session. Without the `dept-<slug>-` prefix,
        # the same session_id would host both the person and the dept as
        # owners, and Honcho's per-peer derived facts would mingle in
        # ways that break "what does <slug> think about X" later.
        # Cardinality note: N_threads × N_depts sessions over the
        # lifetime of an org. Honcho is designed for this — sessions are
        # cheap — and per-session scoping is what gives `peer.chat()`
        # coherent context windows to synthesize from.
        scoped_session = _safe_honcho_id(
            f"dept-{department_slug}-{session_id}"
            if session_id
            else f"dept-{department_slug}"
        )
        sess = await client.aio.session(scoped_session)
        extra_peers: list[Any] = [dept_peer]
        originator_peer: Any | None = None
        if originating_person_id is not None:
            originator_peer = await client.aio.peer(str(originating_person_id))
            extra_peers.append(originator_peer)
        for pid in co_present_person_ids:
            extra_peers.append(await client.aio.peer(str(pid)))
        for slug in co_present_department_slugs:
            extra_peers.append(await client.aio.peer(_department_peer_id(slug)))
        # Dept peer + originator (if any) + co-present extras must all be
        # added to the session: peers that don't author a message in the
        # session aren't automatically added by Honcho, and the dept peer
        # in particular authors nothing here (its representation derives
        # from session participation, not from claiming the inbound).
        await sess.aio.add_peers(extra_peers)
        msgs = []
        # Author the inbound as the originating person when known —
        # attributing to the dept peer would teach Honcho that the dept
        # "says" user words, polluting future peer.chat() results. If no
        # person is known, drop the inbound rather than misattribute it;
        # the assistant_response alone still informs the dept peer via
        # session co-presence.
        if user_message.strip() and originator_peer is not None:
            msgs.append(originator_peer.message(user_message))
        if assistant_response.strip():
            msgs.append(exec_peer.message(assistant_response))
        if msgs:
            await sess.aio.add_messages(msgs)
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=originating_person_id,
            department_slug=department_slug,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "peer_count": 1 + len(extra_peers),  # exec_peer + extras
                "originating_person_id": originating_person_id,
                "co_present_person_ids": co_present_person_ids,
                "co_present_department_slugs": co_present_department_slugs,
                "message_count": len(msgs),
                "session_id": session_id,
            },
        )
    except Exception as exc:
        logger.exception(
            "honcho: sync_department_turn failed for department_slug=%s session_id=%s",
            department_slug,
            session_id,
        )
        _emit_peer_memory(
            op="sync_department_turn",
            person_id=None,
            department_slug=department_slug,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "session_id": session_id,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )


# Allowed `kind` values for ``append_department_note``. Kept as a Literal
# (rather than free-form str) so callers misspelling `"decision"` get a
# mypy error rather than landing a row with an unrecognizable shape that
# clogs the dept peer's representation.
NoteKind = Literal[
    "decision",
    "initiative_update",
    "advice",
    "goal_update",
    "committee_decision",
    "cadence_checkin",
]


def append_department_note(
    *,
    department_slug: str | None,
    kind: NoteKind,
    body: str,
    person_id: int | None = None,
) -> None:
    """Fire-and-forget structured note to a department's Honcho timeline.

    For non-turn events that should still inform the department peer's
    memory: a committee decision landing, a goal/KR update, a scheduled
    cadence check-in summary, or the episodic-store mirror of a decision
    / initiative / advice row whose ``department`` field names this slug.

    The note is pushed as a single executive-peer message into a dedicated
    ``dept-<slug>-notes`` session so kind-tagged events accumulate in one
    timeline the dept peer can synthesize over. When ``person_id`` is
    given, that person joins the session as a co-present peer, wiring the
    note into both representations (analog of the per-turn co-presence
    shape).

    Silent on failure. Honcho is best-effort; the episodic SQL row is the
    system of record.
    """
    if not department_slug:
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            outcome="no_department",
        )
        return
    settings = get_settings()
    if not settings.honcho_enabled:
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            department_slug=department_slug,
            outcome="disabled",
        )
        return
    if not body.strip():
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            department_slug=department_slug,
            outcome="empty",
        )
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            department_slug=department_slug,
            outcome="no_loop",
        )
        return
    # Snapshot the audit ContextVars at scheduling time — see _do_sync
    # for rationale (parent's set_turn block has already exited by the
    # time this background task runs).
    audit_sid, audit_tid = get_active_ids()
    task = loop.create_task(
        _do_append_department_note(
            department_slug, kind, body, person_id, audit_sid, audit_tid,
        )
    )
    _pending_sync_tasks.add(task)
    task.add_done_callback(_pending_sync_tasks.discard)


async def _do_append_department_note(
    department_slug: str,
    kind: NoteKind,
    body: str,
    person_id: int | None,
    audit_session_id: str | None,
    audit_turn_id: str | None,
) -> None:
    # Re-bind the audit ContextVars so peer_memory rows we emit land on
    # the right session in the per-turn flow chart.
    with set_turn(session_id=audit_session_id, turn_id=audit_turn_id):
        await _do_append_department_note_body(
            department_slug, kind, body, person_id,
        )


async def _do_append_department_note_body(
    department_slug: str,
    kind: NoteKind,
    body: str,
    person_id: int | None,
) -> None:
    t0 = time.monotonic()
    client = await _get_client()
    if client is None:
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            department_slug=department_slug,
            outcome="error",
        )
        return
    try:
        dept_peer = await client.aio.peer(_department_peer_id(department_slug))
        exec_peer = await client.aio.peer(_EXECUTIVE_PEER_ID)
        session_id = _safe_honcho_id(f"dept-{department_slug}-notes")
        sess = await client.aio.session(session_id)
        # The dept peer must be in the notes session for Honcho to derive
        # facts into its representation — without this add_peers call the
        # notes would land in a session the dept peer isn't part of and
        # later `peer.chat()` on the dept peer wouldn't see them.
        # Person co-presence (when provided) attributes the note so
        # directional queries can later surface "Alice authored this".
        extras: list[Any] = [dept_peer]
        if person_id is not None:
            extras.append(await client.aio.peer(str(person_id)))
        await sess.aio.add_peers(extras)
        # Prefix the body with the kind so the dept peer's representation
        # extraction sees the category tag inline — Honcho doesn't have a
        # native "kind" field on messages, so the inline tag is what
        # makes "show me all committee decisions" answerable later.
        tagged_body = f"[{kind}] {body}"
        await sess.aio.add_messages([exec_peer.message(tagged_body)])
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            department_slug=department_slug,
            outcome="ok",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "kind": kind,
                "body_chars": len(body),
                "session_id": session_id,
            },
        )
    except Exception as exc:
        logger.exception(
            "honcho: append_department_note failed dept=%s kind=%s",
            department_slug,
            kind,
        )
        _emit_peer_memory(
            op="append_department_note",
            person_id=person_id,
            department_slug=department_slug,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "kind": kind,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )
