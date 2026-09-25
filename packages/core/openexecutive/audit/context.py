"""Audit context — propagates session_id / turn_id to fire-and-forget emit sites.

A single Executive turn fans out across many helpers (retriever, specialist
routing, cache_manager probe, tool dispatch). Threading session_id + turn_id
through every signature would touch ~30 call sites and break unrelated
abstractions. ContextVars give us per-task propagation that's automatic
across `await` boundaries while staying scoped to the current asyncio task —
no global state, no cross-talk between concurrent chat turns.

Usage:

    from openexecutive.audit.context import set_turn, get_active_ids

    with set_turn(session_id="discord:dm:42", turn_id="t-abc"):
        await executive.stream_chat(...)   # retriever et al see the IDs

    sid, tid = get_active_ids()            # outside: both None

The context manager *resets* the vars on exit, so nested or stacked turns
restore prior values cleanly. This is the same pattern used by
`orchestrator.schedule_tools.current_session`.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

# Default to None so call sites that haven't been wrapped (CLI, ad-hoc tools,
# tests) just produce un-tagged events instead of crashing.
_audit_session_id: ContextVar[str | None] = ContextVar(
    "audit_session_id", default=None
)
_audit_turn_id: ContextVar[str | None] = ContextVar(
    "audit_turn_id", default=None
)


def get_active_ids() -> tuple[str | None, str | None]:
    """Return (session_id, turn_id) for the active turn, or (None, None)."""
    return _audit_session_id.get(), _audit_turn_id.get()


def get_active_session_id() -> str | None:
    return _audit_session_id.get()


def get_active_turn_id() -> str | None:
    return _audit_turn_id.get()


def bind_turn(*, session_id: str | None, turn_id: str | None) -> None:
    """Set both ContextVars without using a `with` block.

    For long-running async generators that yield repeatedly, a sync `with
    set_turn(...)` block over the whole body is the safe choice. Use this
    helper only when you also call `clear_turn()` at every exit path —
    otherwise a task that processes a second turn (rare: a workflow that
    piggy-backs on the same asyncio task) will inherit stale IDs and tag
    retrieval/audit rows incorrectly.

    Async generators that may be abandoned mid-stream (e.g., HTTP client
    drops the SSE connection) won't reach explicit `clear_turn()` calls;
    in those cases prefer `with set_turn(...)`.
    """
    _audit_session_id.set(session_id)
    _audit_turn_id.set(turn_id)


def clear_turn() -> None:
    """Reset both audit ContextVars to None. Pair with `bind_turn()`."""
    _audit_session_id.set(None)
    _audit_turn_id.set(None)


@contextlib.contextmanager
def set_turn(
    *, session_id: str | None = None, turn_id: str | None = None
) -> Iterator[None]:
    """Bind session_id + turn_id for the duration of the `with` block.

    Both kwargs are optional so callers can set just one if needed (e.g.
    a workflow that has a session but no logical turn). Reset on exit so
    nested calls restore prior values.

    Implementation note: we *save the prior value and restore it on exit*
    rather than using ``ContextVar.set()``/``reset(token)``. The Token
    variant raises ``ValueError: <Token …> was created in a different
    Context`` when ``__exit__`` runs in a Context that differs from the
    one ``__enter__`` ran in — which happens when this manager wraps an
    ``async for`` over an async generator whose iteration crosses task
    boundaries (FastAPI's SSE driver, httpx-backed streaming, anything
    that suspends/resumes the generator outside the original asyncio
    Task). The save/restore pattern is Context-independent and produces
    the same observable behavior.
    """
    prior_session = _audit_session_id.get()
    prior_turn = _audit_turn_id.get()
    _audit_session_id.set(session_id)
    _audit_turn_id.set(turn_id)
    try:
        yield
    finally:
        _audit_session_id.set(prior_session)
        _audit_turn_id.set(prior_turn)


# Rows kept private to the principal by the stretch of work that writes them,
# not only by the session bound when they are written
# (`people_tools.audit_row_private_to_principal` reads both):
# - `private_rows`: every row. The email poller holds it for the whole
#   handling of a mail from one of the principal's contacts or a mail they
#   forwarded, because some of its rows (the knowledge retrieval, for one)
#   are written before the turn binds its private session.
# - `principal_turn_rows`: rows that name one of the principal's contacts,
#   as on the principal's own verified turn. A chat adapter holds it from
#   knowing the principal sent the message until its handling ends, for the
#   rows written before the turn binds its session (the inbound row, the
#   knowledge retrieval, alert triage).
# A task or an `asyncio.to_thread` call started inside inherits the scope
# (both copy the context), so a background pass stays private too; a bare
# `threading.Thread` would not. Neither scope changes who the turn may reach:
# that stays with the bound session. Unattended work started inside (a resumed
# workflow run, a scheduled action) clears both with `unscoped_audit_rows`.
_rows_private: ContextVar[bool] = ContextVar("audit_rows_private", default=False)
_rows_on_principal_turn: ContextVar[bool] = ContextVar(
    "audit_rows_on_principal_turn", default=False
)


def rows_private() -> bool:
    """Whether every audit row written now is private to the principal."""
    return _rows_private.get()


def rows_on_principal_turn() -> bool:
    """Whether audit rows written now are on the principal's own turn."""
    return _rows_on_principal_turn.get()


@contextlib.contextmanager
def private_rows(active: bool = True) -> Iterator[None]:
    """Keep every audit row written inside the block private to the principal
    when ``active``. An inner block never lifts an outer one. Save/restore, as
    in ``set_turn``."""
    prior = _rows_private.get()
    _rows_private.set(prior or bool(active))
    try:
        yield
    finally:
        _rows_private.set(prior)


@contextlib.contextmanager
def principal_turn_rows(active: bool = True) -> Iterator[None]:
    """Treat audit rows written inside the block as on the principal's own
    verified turn when ``active``: one that names a contact is private. An
    inner block never lifts an outer one. Save/restore, as in ``set_turn``."""
    prior = _rows_on_principal_turn.get()
    _rows_on_principal_turn.set(prior or bool(active))
    try:
        yield
    finally:
        _rows_on_principal_turn.set(prior)


@contextlib.contextmanager
def unscoped_audit_rows() -> Iterator[None]:
    """Clear both scopes above for the block, and put them back after.

    For unattended work started from inside a scoped stretch — a workflow
    run a chat reply resumes, a scheduled action: it copies the caller's
    context but is not part of that turn, so its rows must follow its own
    rule. Inherited, the principal's scope would hide every row of the run
    that names a contact, and whoever started the run would notice the gap.
    Save/restore, as in ``set_turn``."""
    prior_private = _rows_private.get()
    prior_principal = _rows_on_principal_turn.get()
    _rows_private.set(False)
    _rows_on_principal_turn.set(False)
    try:
        yield
    finally:
        _rows_private.set(prior_private)
        _rows_on_principal_turn.set(prior_principal)
