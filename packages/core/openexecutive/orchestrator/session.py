from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openexecutive.memory.company_profile import CompanyProfile


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    company_profile: CompanyProfile | None = None
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    # (channel, channel_ref) pairs the Executive has seen during this session.
    # Used by schedule_followup to refuse scheduling sends to refs the user
    # never actually used — anti-spam guard.
    seen_channel_refs: set[tuple[str, str]] = field(default_factory=set)
    # People-roster id of the human speaking this turn (None when unresolved).
    # Lets tool handlers authorize actions that are only valid for that person.
    speaker_person_id: int | None = None

    def add_user_message(self, content: str) -> None:
        self.conversation_history.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str | list[dict[str, Any]]) -> None:
        self.conversation_history.append({"role": "assistant", "content": content})

    def get_recent_history(self, max_turns: int = 20) -> list[dict[str, Any]]:
        history = self.conversation_history[-(max_turns * 2):]
        # Anthropic requires messages to start with a user turn.
        # Drop a leading assistant message if history length is odd (can happen on error recovery).
        if history and history[0]["role"] != "user":
            history = history[1:]
        return history
