"""Whose a follow-up or a piece of advice is, for the routes and
MCP resources that list them (the Pulse page reads these).

The rule is the Activity feed's (``api.routes.today._Owners``): a row is one
person's when it came from their chat, was made by or assigned to them, or is
a message to them that no department sent. Everything else is the team's.
A reader gets the team's rows and their own, the principal included.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

logger = logging.getLogger(__name__)


class Reader:
    """One caller's view. ``viewer`` is their People id; None (signed in but
    not on the roster, a service call) reads the team's rows only."""

    def __init__(self, viewer: int | None) -> None:
        from openexecutive.api.routes.today import _Owners

        self.viewer = viewer
        # Not caught: with no owners read, one person's rows would read as
        # the team's. The read fails instead, as the Activity feed does.
        self._owners = _Owners()

    def _theirs(self, owner: int | None) -> bool:
        return owner is None or owner == self.viewer

    def sees_action(self, action: Any) -> bool:
        # Made by them or assigned to them: either makes it theirs to see.
        if self.viewer is not None and self.viewer in (
            action.created_by_person_id, action.assigned_to_person_id,
        ):
            return True
        return self._theirs(self._owners.of_action(action))

    def sees_session_row(self, row: Any) -> bool:
        """An advice row, by the chat it came from."""
        return self._theirs(
            self._owners.of_session(row.session_id or None, department=row.department or None)
        )


def reader_for(request: Request) -> Reader:
    """The signed-in caller's view. A request with no caller header (the CLI,
    local dev) is the principal's, as everywhere else."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    try:
        viewer = _resolve_caller_person_id(request)
    except Exception:
        logger.exception("own_rows: caller lookup failed, showing the team's rows only")
        viewer = None
    return Reader(viewer)
