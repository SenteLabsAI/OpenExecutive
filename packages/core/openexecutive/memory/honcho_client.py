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
import unicodedata
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

# Honcho's dialectic latency scales steeply with ``reasoning_level``. Measured
# against hosted Honcho on a warm workspace, same query: ``low`` ~2.1s,
# ``minimal`` ~2.8s, ``medium`` ~8.1s. A single flat budget therefore makes the
# deeper levels unreachable — a caller that asks for ``medium`` under the
# default 3s budget times out *every* time and gets no peer memory at all,
# which is strictly worse than having asked for ``low``. And because prefetch
# degrades silently (returns "" on timeout), the turn just quietly runs
# memory-less; nothing surfaces the fact that the level is unusable.
#
# So scale the operator-configured base budget by level. The base stays the
# knob for the fast per-turn path (``HONCHO_PREFETCH_TIMEOUT_S``); the
# multipliers give the deliberate, deeper levels a budget they can meet.
# ``minimal``/``low`` deliberately stay at 1.0: the base IS the knob for the
# fast per-turn path, and scaling it there would inflate the inline stall on
# every ordinary turn. (``minimal`` measured marginally slower than ``low``,
# which argues for a little headroom — but that is an argument for raising the
# base, not for silently multiplying it. A multiplier above 1.0 here also
# collapses the whole table against the ceiling at a large base.)
# ``medium`` is 4.0 rather than the ~2.7 its 8.1s measurement alone implies:
# the budget spans ``peer()`` + ``chat()``, not just the dialectic call, and a
# budget set at the mean still times out on half the calls.
# ``high``/``max`` are extrapolated — no OE call site requests them yet, so
# there is nothing to measure. Revisit with real numbers before relying on
# either.
_REASONING_TIMEOUT_MULTIPLIER: dict[str, float] = {
    "minimal": 1.0,
    "low": 1.0,
    "medium": 4.0,
    "high": 5.0,
    "max": 6.0,
}

# Absolute ceiling for one prefetch, whatever the configured base. `prefetch`
# is awaited *inline* in the turn (before the first SSE byte), so the scaling
# above must not turn a generous base into a user-visible stall: at a 10s base
# an unbounded ``max`` would reach 60s of dead air. It caps only the scaling
# and never drops the budget below the operator's configured base — which is
# also why the fast levels sit at 1.0: were they scaled, a base at or above
# the ceiling would flatten every level onto it and the operator's knob would
# stop distinguishing the per-turn path at all.
_PREFETCH_CEILING_S = 15.0

# Fallback when the configured base is non-positive. ``asyncio.wait_for`` with
# a timeout <= 0 fires before the request is made, so a misconfigured 0 would
# turn every prefetch into an instant silent timeout.
_DEFAULT_PREFETCH_TIMEOUT_S = 3.0

# Ceiling for `directional_chat`, which backs a deliberate tool call rather
# than the per-turn budget path.
_DIRECTIONAL_TIMEOUT_S = 30.0

# Ceiling for one whole fire-and-forget sync. The SDK client timeout used to be
# the only bound on these bodies (none of their calls is individually
# wait_for'd) and raising it to an outer bound removed even that — with the
# SDK's own retries, a blackholing endpoint could otherwise pin a task, and the
# connections it holds on the shared client, for minutes.
_SYNC_TOTAL_TIMEOUT_S = 60.0

# Kept strictly greater than every per-call budget rather than merely equal,
# so which bound fires first is by construction and not by coincidence.
_CLIENT_TIMEOUT_HEADROOM_S = 5.0


def prefetch_timeout_s(reasoning_level: ReasoningLevel, base_timeout_s: float) -> float:
    """The wall-clock budget for one dialectic prefetch at ``reasoning_level``."""
    base = base_timeout_s if base_timeout_s > 0 else _DEFAULT_PREFETCH_TIMEOUT_S
    scaled = base * _REASONING_TIMEOUT_MULTIPLIER.get(reasoning_level, 1.0)
    return max(base, min(scaled, _PREFETCH_CEILING_S))


def _client_timeout_s(base_timeout_s: float) -> float:
    """HTTP timeout for the SDK client.

    An **outer** bound only: every call path applies its own, tighter
    ``asyncio.wait_for`` budget on top. It must therefore be longer than the
    longest of those budgets, or the transport aborts first and the per-call
    budget never gets to apply. That was previously the case — the client was
    built with the bare prefetch budget (3s by default), silently capping
    `directional_chat` far below its documented 30s ceiling.
    """
    # Single-call budgets only (plus the per-peer identity budget, which
    # spans two requests each shorter than it). The whole-pass ceilings
    # (_SYNC_TOTAL_TIMEOUT_S, _SEED_TOTAL_TIMEOUT_S, _SESSION_PURGE_BUDGET_S)
    # each span several sequential requests; folding them in would hand every
    # single request — including the ones no wait_for covers — an outer bound
    # several times longer than any one call is ever allowed to take.
    longest = max(
        [
            _DIRECTIONAL_TIMEOUT_S,
            _SESSION_DELETE_TIMEOUT_S,
            _WORKSPACE_DELETE_TIMEOUT_S,
            _IDENTITY_SEED_TIMEOUT_S,
        ]
        + [
            prefetch_timeout_s(level, base_timeout_s)  # type: ignore[arg-type]
            for level in _REASONING_TIMEOUT_MULTIPLIER
        ]
    )
    return longest + _CLIENT_TIMEOUT_HEADROOM_S

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
                timeout=_client_timeout_s(settings.honcho_prefetch_timeout_s),
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
    _identity_seeded.clear()
    _identity_failed.clear()


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
    async def _list_all() -> None:
        page = await client.aio.sessions()
        async for session in page:
            sessions.append(session)

    try:
        # The listing pages transparently and each page request now gets the
        # (wide) client timeout, so it needs the same ceiling as the deletes.
        await asyncio.wait_for(_list_all(), timeout=_SESSION_PURGE_BUDGET_S)
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
            timeout=_client_timeout_s(settings.honcho_prefetch_timeout_s),
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


# Peer cards Honcho has been told the identity for:
# ``(workspace_id, person_id) -> (facts written, monotonic time verified)``.
# Keyed on the workspace the *writing client* is bound to rather than the
# ambient override (the two can diverge across processes), and bounded by
# roster size rather than growing once per roster edit.
_identity_seeded: dict[tuple[str, int], tuple[tuple[str, ...], float]] = {}

# Honcho's dreaming agent keeps re-deriving identity from the peer id, and it
# runs on every message the sync pushes — so one successful seed is not
# permanent; it can put ``IDENTITY: Name: 1`` straight back. Re-verify
# periodically rather than memoizing "done" for the life of the process.
_IDENTITY_RECHECK_S = 900.0

# Per-peer ceiling for the two card round trips. The sync path is
# fire-and-forget, so absent this the only bound is the SDK client timeout.
_IDENTITY_SEED_TIMEOUT_S = 10.0

# Ceiling for a whole seeding pass. The per-peer bound alone leaves the serial
# loop costing peers x budget, which a wide thread turns into a pile-up.
_SEED_TOTAL_TIMEOUT_S = 20.0

# Peers whose last seed attempt failed, and when. A degraded card endpoint is
# then retried once per interval instead of once per peer per turn.
_identity_failed: dict[tuple[str, int], float] = {}
_IDENTITY_RETRY_AFTER_S = 120.0

_IDENTITY_PREFIX = "IDENTITY:"


def _is_identity_line(line: str) -> bool:
    """Whether a card line is one of ours to replace.

    Case-insensitive and whitespace-tolerant: nothing pins the deriver's exact
    formatting, and a near-miss would be worse than a miss — the stale line
    would survive into ``kept`` and the card would then assert two identities
    at once, both of which reach the dialectic prompt.
    """
    return line.strip().upper().startswith(_IDENTITY_PREFIX)


# A card value is reduced to a conservative allow-list rather than screened by
# a deny-list of structure tokens. The consumer here is an LLM system prompt,
# not a strict parser, so semantic equivalence is what matters and exact-match
# screening does not provide a boundary: ``ATTRIBUTE :``, a fullwidth colon, a
# zero-width space inside the word and a Cyrillic homoglyph all read the same
# to the model while each defeating a substring test. Allowing letters, marks,
# digits, spaces and a small punctuation set instead refuses every colon
# variant in one rule — and the colon is what gives a card line its structure.
#
# This does not, and cannot, stop a value that is merely persuasive free text
# ("Ignore prior notes..."); OE already embeds ``full_name`` in its own system
# prompt, so that exposure is pre-existing. What it does stop is roster text
# forging card *structure* in a third party's prompt.
_CARD_ALLOWED_PUNCT = frozenset(" .,'-&()/")
_CARD_ALLOWED_CATEGORIES = frozenset(
    {"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Me", "Nd", "Nl", "No"}
)
# Colon lookalikes that the category allow-list would otherwise admit — they
# are letters, marks or numerals by Unicode category and survive NFKC — so
# they are named explicitly. Lm cannot simply be dropped: it also holds
# U+3005 々 and U+30FC ー (as in 佐々木, ジョーンズ) and U+02BB ʻ (Hawaiʻi),
# which real names need.
#
# Source: Unicode's confusables data (every character it maps to U+003A),
# intersected with the allowed categories — NOT a scan of character names,
# which misses colon-shaped characters whose names never say "colon", such as
# U+A4FD LISU LETTER TONE MYA JEU. The visarga marks are included on the same
# basis: they render as a stacked pair of dots.
#
# These are STRIPPED, not grounds for refusing the value. The visarga is the
# nominative ending in formal Devanagari and Gujarati names (रामः, નરેશઃ), and
# refusing the whole value would leave those people never seeded — the exact
# defect this feature fixes. Stripping keeps the name (as its stem form) and
# still guarantees no colon shape reaches the card; whatever prose remains is
# the free-text exposure documented above. Two tests guard this: an exhaustive
# scan of every COLON-named codepoint, and the explicit confusables list.
#
# Cross-checked against confusables.txt 18.0.0: of the 33 sources it maps to
# U+003A, 12 survive the category allow-list and all 12 are here. The one
# source deliberately absent is U+FE30 (vertical two-dot leader), which NFKC
# folds to two plain periods *before* this check runs — what reaches the
# card is "..", not a colon shape, and denying "." would refuse real names
# like "J. R. R. Tolkien". Re-run the cross-check when Unicode updates.
_CARD_COLON_LOOKALIKES = frozenset(
    "\u02d0\u02d1"            # modifier letter (half) triangular colon
    "\U00010781\U00010782"    # their superscript forms (NFKC-fold to the above)
    "\ua4fd"                   # Lisu letter tone mya jeu
    "\u0903\u0a83"            # Devanagari / Gujarati visarga
    "\U00011002\U00011082\U00011182\U000115be\U000116ac\U00011838"  # Brahmi, Kaithi, Sharada, Siddham, Takri, Dogra visargas
    "\U0001015b"               # Greek acrophonic Epidaurean two
)

_CARD_VALUE_MAX_CHARS = 120

# Cap on the deriver-authored lines carried over by a re-seed, so a card
# cannot grow without bound through repeated read-modify-write passes.
_CARD_KEPT_MAX_LINES = 40


def _card_value(raw: str) -> str | None:
    """A roster string reduced to something safe to put on a card, or ``None``
    when it cannot be.

    Card lines are injected verbatim into Honcho's dialectic system prompt,
    and roster values are attacker-reachable: the Executive exposes
    ``upsert_person`` as a chat tool, so anyone who can talk to it — an unknown
    inbound email sender included — can propose a name.
    """
    # NFKC first, so compatibility forms (fullwidth latin, fullwidth colon,
    # ligatures) fold to their plain equivalents before anything is checked.
    text = unicodedata.normalize("NFKC", raw)
    # Whitespace runs collapse to one space *before* controls are dropped, so
    # a newline becomes a separator rather than vanishing and joining two
    # words together.
    text = " ".join(text.split())
    # Remaining control and format characters (zero-width joiners, bidi
    # overrides) are invisible and only ever used to smuggle.
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf"))
    # Colon lookalikes are removed rather than refused — see the note on
    # _CARD_COLON_LOOKALIKES. A real name survives minus the mark; a forged
    # "ATTRIBUTEː" loses the one character that gave it structure.
    text = "".join(c for c in text if c not in _CARD_COLON_LOOKALIKES)
    flat = " ".join(text.split())[:_CARD_VALUE_MAX_CHARS].strip()
    if not flat:
        return None
    for char in flat:
        if char in _CARD_ALLOWED_PUNCT:
            continue
        if unicodedata.category(char) in _CARD_ALLOWED_CATEGORIES:
            continue
        return None
    return flat


def _identity_lines(person: Any) -> list[str]:
    """The card lines OE owns for a Person — deliberately the name alone.

    The name is what fixes the defect. Adding email or role would export
    roster PII to a third-party memory service, for co-present peers who never
    interacted with the Executive at that, and buys no additional correctness:
    the deriver already picks role and domain up from conversation content.
    """
    name = _card_value(person.full_name)
    return [] if name is None else [f"{_IDENTITY_PREFIX} Name: {name}"]


async def _seed_peer_identity(
    peer: Any, person_id: int, person: Any, *, workspace_id: str
) -> bool:
    """Tell Honcho who this peer actually is. Returns whether it wrote.

    Peers are keyed by ``Person.id``, so absent this the only identity signal
    Honcho ever receives is a bare integer — and its dreaming agent duly
    concludes that the number *is* the person's name, producing cards that
    read ``IDENTITY: Name: 1``. Peer cards are injected into the dialectic
    system prompt on every ``peer.chat()``, so that error then propagates into
    every answer the Executive gets back about that person.

    ``set_card`` replaces the whole card, so keep the deriver's own
    ``ATTRIBUTE:`` / ``RELATIONSHIP:`` lines and swap only the ``IDENTITY:``
    ones — those are the lines the roster knows better. The existing card is
    split on physical lines first: a single list element may itself span
    lines, and a start-anchored test on the element would let an identity
    assertion on its second line survive into ``kept`` and be re-persisted
    forever, which is the two-identities state this is meant to prevent.

    Known limitation: the replace is read-modify-write and the SDK exposes no
    version or ETag, so a deriver write landing between the read and the write
    is lost. Self-correcting (the deriver re-derives) and not fixable
    client-side with the current API.
    """
    if person is None:
        return False
    desired = _identity_lines(person)
    if not desired:
        return False
    facts = tuple(desired)
    key = (workspace_id, person_id)
    now = time.monotonic()
    failed_at = _identity_failed.get(key)
    if failed_at is not None and (now - failed_at) < _IDENTITY_RETRY_AFTER_S:
        # A degraded card endpoint gets probed once per interval, not once per
        # peer per turn: without this, every inbound message re-pays the full
        # per-peer budget for every peer for as long as Honcho is unwell.
        return False
    seen = _identity_seeded.get(key)
    if seen is not None and seen[0] == facts and (now - seen[1]) < _IDENTITY_RECHECK_S:
        return False
    existing = [
        sub.strip()
        for line in ((await peer.aio.get_card()) or [])
        for sub in str(line).splitlines()
        if sub.strip()
    ]
    if [ln for ln in existing if _is_identity_line(ln)] == desired:
        _identity_seeded[key] = (facts, time.monotonic())
        _identity_failed.pop(key, None)
        return False
    # The deriver appends newest-last, so cap from the front: dropping the
    # oldest lines is a bounded loss, dropping the newest would erase every
    # correction it made since the last seed.
    kept = [ln for ln in existing if not _is_identity_line(ln)][-_CARD_KEPT_MAX_LINES:]
    await peer.aio.set_card(desired + kept)
    _identity_seeded[key] = (facts, time.monotonic())
    _identity_failed.pop(key, None)
    return True


async def _seed_identities(
    client: Any, targets: list[tuple[int, Any]]
) -> tuple[int, str, int]:
    """Best-effort identity seeding for each ``(person_id, peer)``.

    Bounded per peer *and* in aggregate, and never allowed to raise: this runs
    after the message write, whose success is the actual point of the sync.
    The aggregate bound matters because the pass is serial — without it a
    hanging card endpoint costs ``len(targets) x`` the per-peer budget on every
    single turn, which a thread with many resolvable participants turns into a
    pile-up on the shared client.

    Returns ``(peers written, outcome, peers failed)``. Outcome is ``ok`` only
    when every peer's attempt finished without raising (whatever it wrote),
    ``timeout`` when the pass or the roster read hit its ceiling, and
    ``error`` when any peer's own attempt raised or hit its budget.
    """
    if not targets:
        return 0, "empty", 0
    # One entry per person, first occurrence wins: `completed` is keyed on
    # person_id, so a duplicated target (the department path can receive
    # the originator again among the co-present ids) would let a cut-off
    # second attempt hide behind the first's completion.
    unique: dict[int, Any] = {}
    for person_id, peer in targets:
        unique.setdefault(person_id, peer)
    targets = list(unique.items())
    try:
        workspace_id = getattr(client, "workspace_id", "") or get_active_workspace_id()
    except Exception:
        # Nothing here may escape: the caller has already written the messages
        # and a raise would audit the whole sync as failed after it succeeded.
        logger.warning("honcho: could not resolve workspace for identity seeding")
        return 0, "error", 0
    from openexecutive.people.store import get_person

    # Roster reads first, in one hop off the loop. ``asyncio.wait_for`` cannot
    # cancel a worker thread, so a blocking SQLite read must not sit inside the
    # per-peer budget that is supposed to bound it.
    # Bounded so the TASK cannot sit here indefinitely. The worker thread
    # itself is not cancellable; the roster connection has SQLite's default
    # 5s busy timeout per read, so a locked table costs the thread at most
    # ~N x 5s before it raises, while the task detaches at the budget.
    try:
        people = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: {person_id: get_person(person_id) for person_id, _ in targets}
            ),
            timeout=_IDENTITY_SEED_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("honcho: roster lookup for identity seeding failed (%s)", type(exc).__name__)
        return 0, ("timeout" if isinstance(exc, TimeoutError) else "error"), 0
    seeded: list[int] = []
    failed_now: list[int] = []
    # Every target whose attempt FINISHED — written, already correct, skipped
    # by a memo, or failed. Deliberately not a ``finally``: a peer cut off
    # mid-flight by the pass ceiling must stay out of this set.
    completed: set[int] = set()

    async def _pass() -> None:
        for person_id, peer in targets:
            try:
                wrote = await asyncio.wait_for(
                    _seed_peer_identity(
                        peer,
                        person_id,
                        people.get(person_id),
                        workspace_id=workspace_id,
                    ),
                    timeout=_IDENTITY_SEED_TIMEOUT_S,
                )
            except Exception as exc:
                _identity_failed[(workspace_id, person_id)] = time.monotonic()
                completed.add(person_id)
                failed_now.append(person_id)
                # Deliberately not ``exc_info=True``: a 4xx body from the card
                # endpoint typically echoes the offending value back, which
                # would put roster content into application logs.
                logger.warning(
                    "honcho: could not seed peer identity for person_id=%s (%s)",
                    person_id,
                    type(exc).__name__,
                )
                continue
            completed.add(person_id)
            if wrote:
                seeded.append(person_id)

    outcome = "ok"
    try:
        await asyncio.wait_for(_pass(), timeout=_SEED_TOTAL_TIMEOUT_S)
    except Exception as exc:
        outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
        # The per-peer handler cannot see this cancellation, so the peer in
        # flight and every peer never reached would otherwise be re-attempted
        # in full next turn — converging only over ceil(N/2) turns, each
        # paying the whole ceiling. Mark exactly the ones that did not finish.
        now = time.monotonic()
        for person_id, _ in targets:
            if person_id in completed:
                continue
            # Do not mark a peer whose card is already known-correct for the
            # current roster facts within the recheck interval: its attempt
            # would have been an instant memo no-op, so suppressing the retry
            # gains nothing and would block a rename for the whole retry
            # window. This also covers the cancellation that lands after a
            # write returned but before the peer was recorded as completed —
            # the fresh memo entry is the evidence it succeeded.
            seen = _identity_seeded.get((workspace_id, person_id))
            person = people.get(person_id)
            desired = tuple(_identity_lines(person)) if person is not None else ()
            if (
                seen is not None
                and seen[0] == desired
                and (now - seen[1]) < _IDENTITY_RECHECK_S
            ):
                continue
            _identity_failed[(workspace_id, person_id)] = now
        logger.warning(
            "honcho: identity seeding pass aborted (%s)", type(exc).__name__
        )
    if outcome == "ok" and failed_now:
        # ``ok`` means nothing went wrong. A pass in which any peer's own
        # attempt raised or hit its budget is degraded, and nine broken peers
        # out of ten must not read the same as ten already-correct ones.
        outcome = "error"
    return len(seeded), outcome, len(failed_now)


async def _seed_and_audit(
    targets: list[tuple[int, Any]],
    *,
    person_id: int | None,
    session_id: str | None,
    department_slug: str | None = None,
) -> None:
    """Identity seeding for a sync that has already persisted, audited as its
    own ``seed_identity`` row. Kept apart from the sync's row and outside the
    sync ceiling so that row stays a pure record of the persist: however slow
    the card round trips are, they can neither delay nor replace it. Bounded
    entirely by the seeding pass's own per-peer and per-pass ceilings."""
    t0 = time.monotonic()
    # One entry per person (first occurrence wins) BEFORE anything counts
    # them, so the reported `targets` and the pass's own arithmetic agree.
    # The department path can hand the originator back among the co-present
    # ids; a sender cc'd under a second address resolving to the same Person
    # does the same on the person path.
    # NB: the loop variable must not be named person_id — that is this
    # function's keyword parameter (the originator), and rebinding it here
    # attributed every seed row to whichever target happened to come last.
    unique: dict[int, Any] = {}
    for target_id, peer in targets:
        unique.setdefault(target_id, peer)
    targets = list(unique.items())
    client = await _get_client() if targets else None
    if not targets:
        seeded, outcome, failed = 0, "empty", 0
    elif client is None:
        seeded, outcome, failed = 0, "error", 0
    else:
        seeded, outcome, failed = await _seed_identities(client, targets)
    # Emitted on every path, ``empty`` included, so audit absence keeps
    # meaning "the code path never ran" rather than "nothing to do".
    _emit_peer_memory(
        op="seed_identity",
        person_id=person_id,
        department_slug=department_slug,
        outcome=outcome,
        duration_ms=int((time.monotonic() - t0) * 1000),
        details={
            "identity_seeded": seeded,
            # Peers whose attempt raised or hit its budget this pass. Partial
            # degradation is visible here rather than only at 100% failure.
            "failed": failed,
            "targets": len(targets),
            "session_id": session_id,
        },
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

    Times out at ``prefetch_timeout_s(reasoning_level, ...)`` — the
    configured ``Settings.honcho_prefetch_timeout_s`` scaled for the level,
    so a deeper level gets a budget it can actually meet; on any failure
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
    budget_s = prefetch_timeout_s(reasoning_level, settings.honcho_prefetch_timeout_s)
    try:
        answer = await asyncio.wait_for(
            _do_prefetch(client, query, person_id, reasoning_level),
            timeout=budget_s,
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
                "timeout_s": budget_s,
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
            details={"reasoning_level": reasoning_level, "timeout_s": budget_s},
        )
        return ""
    except Exception:
        logger.exception("honcho: prefetch failed for person_id=%s", person_id)
        _emit_peer_memory(
            op="prefetch",
            person_id=person_id,
            outcome="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={"reasoning_level": reasoning_level, "timeout_s": budget_s},
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

    Bounded at ``_DIRECTIONAL_TIMEOUT_S``. Longer than any prefetch budget
    (which is capped at ``_PREFETCH_CEILING_S``) because the model
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
            timeout=_DIRECTIONAL_TIMEOUT_S,
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
        targets: list[tuple[int, Any]] = []
        try:
            await asyncio.wait_for(
                _do_sync_body(
                    user_message, assistant_response, person_id, session_id,
                    co_present_person_ids, targets,
                ),
                timeout=_SYNC_TOTAL_TIMEOUT_S,
            )
        except TimeoutError:
            # None of the body's individual calls is wait_for'd, so this is the
            # only bound on the whole pass now that the client timeout is an
            # outer bound rather than a tight one.
            logger.warning(
                "honcho: sync_turn exceeded %ss for person_id=%s",
                _SYNC_TOTAL_TIMEOUT_S, person_id,
            )
            _emit_peer_memory(
                op="sync_turn",
                person_id=person_id,
                outcome="timeout",
                details={"session_id": session_id, "timeout_s": _SYNC_TOTAL_TIMEOUT_S},
            )
            return
        await _seed_and_audit(targets, person_id=person_id, session_id=session_id)


async def _do_sync_body(
    user_message: str,
    assistant_response: str,
    person_id: int,
    session_id: str | None,
    co_present_person_ids: list[int],
    seed_targets_out: list[tuple[int, Any]],
) -> None:
    """Persist the exchange. On success the peers to seed are appended to
    ``seed_targets_out`` for the caller to run OUTSIDE the sync ceiling."""
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
        extra_peers: list[Any] = []
        seed_targets: list[tuple[int, Any]] = [(person_id, user_peer)]
        for pid in co_present_unique:
            co_peer = await client.aio.peer(str(pid))
            extra_peers.append(co_peer)
            seed_targets.append((pid, co_peer))
        if extra_peers:
            await sess.aio.add_peers(extra_peers)
        msgs = []
        if user_message.strip():
            msgs.append(user_peer.message(user_message))
        if assistant_response.strip():
            msgs.append(exec_peer.message(assistant_response))
        if msgs:
            await sess.aio.add_messages(msgs)
        # The exchange is persisted: record that NOW, before anything else can
        # run. Seeding happens in the caller, outside this body and outside
        # the sync ceiling, precisely so a slow card round trip can never turn
        # a successful persist into a missing or "timeout" audit row.
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
        seed_targets_out.extend(seed_targets)
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
    budget_s = prefetch_timeout_s(reasoning_level, settings.honcho_prefetch_timeout_s)
    try:
        answer = await asyncio.wait_for(
            _do_prefetch_department(client, query, department_slug, reasoning_level),
            timeout=budget_s,
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
                "timeout_s": budget_s,
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
            details={"reasoning_level": reasoning_level, "timeout_s": budget_s},
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
            details={"reasoning_level": reasoning_level, "timeout_s": budget_s},
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
        targets: list[tuple[int, Any]] = []
        try:
            await asyncio.wait_for(
                _do_sync_department_body(
                    user_message, assistant_response, department_slug, session_id,
                    originating_person_id, co_present_person_ids,
                    co_present_department_slugs, targets,
                ),
                timeout=_SYNC_TOTAL_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "honcho: sync_department_turn exceeded %ss for department_slug=%s",
                _SYNC_TOTAL_TIMEOUT_S, department_slug,
            )
            _emit_peer_memory(
                op="sync_department_turn",
                person_id=originating_person_id,
                department_slug=department_slug,
                outcome="timeout",
                details={"session_id": session_id, "timeout_s": _SYNC_TOTAL_TIMEOUT_S},
            )
            return
        await _seed_and_audit(
            targets,
            person_id=originating_person_id,
            session_id=session_id,
            department_slug=department_slug,
        )


async def _do_sync_department_body(
    user_message: str,
    assistant_response: str,
    department_slug: str,
    session_id: str | None,
    originating_person_id: int | None,
    co_present_person_ids: list[int],
    co_present_department_slugs: list[str],
    seed_targets_out: list[tuple[int, Any]],
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
        # Person peers on this path are created from a bare Person.id exactly
        # as they are on the person path, so they need the same identity seed
        # — otherwise a person who only ever appears in a department turn
        # keeps an integer for a name.
        seed_targets: list[tuple[int, Any]] = []
        if originating_person_id is not None:
            originator_peer = await client.aio.peer(str(originating_person_id))
            extra_peers.append(originator_peer)
            seed_targets.append((originating_person_id, originator_peer))
        for pid in co_present_person_ids:
            co_peer = await client.aio.peer(str(pid))
            extra_peers.append(co_peer)
            seed_targets.append((pid, co_peer))
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
        # As in _do_sync_body: the persist row goes out immediately; seeding
        # runs in the caller, outside the sync ceiling.
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
        seed_targets_out.extend(seed_targets)
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
        try:
            await asyncio.wait_for(
                _do_append_department_note_body(
                    department_slug, kind, body, person_id,
                ),
                timeout=_SYNC_TOTAL_TIMEOUT_S,
            )
        except TimeoutError:
            # Same reasoning as _do_sync: the client timeout is an outer bound
            # now, so this body needs its own ceiling or a blackholing
            # endpoint pins the task and its pool connections.
            logger.warning(
                "honcho: append_department_note exceeded %ss for department_slug=%s",
                _SYNC_TOTAL_TIMEOUT_S, department_slug,
            )
            _emit_peer_memory(
                op="append_department_note",
                person_id=person_id,
                department_slug=department_slug,
                outcome="timeout",
                details={"kind": kind, "timeout_s": _SYNC_TOTAL_TIMEOUT_S},
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
