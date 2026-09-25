"""Persistent, searchable audit log for the Open Executive system.

Captures valuable, non-verbose events (chat turns, specialist consultations,
tool invocations, scheduled actions, alerts, inbound integration events)
into a SQLite table for later review and search.
"""
from openexecutive.audit.context import (
    bind_turn,
    clear_turn,
    get_active_ids,
    get_active_session_id,
    get_active_turn_id,
    principal_turn_rows,
    private_rows,
    set_turn,
    unscoped_audit_rows,
)
from openexecutive.audit.logger import (
    AuditEvent,
    AuditLogger,
    get_audit_logger,
    log_event,
    set_audit_logger,
)

__all__ = [
    "AuditEvent",
    "AuditLogger",
    "bind_turn",
    "clear_turn",
    "get_active_ids",
    "get_active_session_id",
    "get_active_turn_id",
    "get_audit_logger",
    "log_event",
    "principal_turn_rows",
    "private_rows",
    "set_audit_logger",
    "set_turn",
    "unscoped_audit_rows",
]
