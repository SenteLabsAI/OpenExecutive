from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from openexecutive.memory.company_profile import CompanyProfile

if TYPE_CHECKING:
    from openexecutive.memory.workspace_settings import PrincipalRole


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
    # Which inbound chat channel this session arrived on ("slack", "discord",
    # "telegram", "google_chat"), and the address on it. Empty for web/CLI
    # turns. Used when a workflow raises an approval gate mid-conversation:
    # the gate records where to look for the answer, so a reply on this
    # channel can resolve it. Inbound vocabulary — see `normalize_channel`.
    origin_channel: str = ""
    origin_channel_ref: str = ""
    # True only for a session minted by the web chat route. `origin_channel`
    # cannot stand in for this: it names an INBOUND CHAT ADAPTER, and the email
    # poller, alert review, the CLI, the MCP server, the scheduler and the
    # unattended workflows all leave it empty while being nothing like a
    # browser turn. Anything that wants to treat browser turns differently has
    # to ask for them by name.
    from_web_chat: bool = False
    # The rostered Person behind this conversation, when one is resolved. The
    # adapters already pass this to `Executive.chat(person_id=...)`; holding it
    # on the session too lets tool handlers running mid-turn tell "the approver
    # is the person I'm already talking to" from "the approver is someone else".
    caller_person_id: int | None = None
    # The live alert board as the server derived it this turn, recorded by
    # `briefing.context.render_and_trust`. `ack_alert` refuses anything else on
    # EVERY session, web included, so an id quoted inside an alert's own body —
    # alerts are minted from inbound mail and chat, so that text is
    # attacker-controlled — cannot clear a row that is closed, snoozed or
    # invented. It does not stop the model being argued into acking the wrong
    # LIVE card; see `format_open_alerts_for_prompt` for the limits of this
    # control. Empty means the turn was shown no board and can ack nothing.
    trusted_alert_ids: set[int] = field(default_factory=set)
    # Per-session override of the install's workspace mode ("solo" / "team";
    # None = use the workspace setting). Evals run scenarios concurrently on
    # one Executive, so they set it here instead of flipping the global. Read
    # it through `memory.workspace_settings.effective_workspace_mode`.
    workspace_mode: str | None = None
    # Per-session override of the principal's role (None = use the
    # workspace's), for the same reason: an eval scenario supplies the role
    # of the principal it plays without writing the install-wide row. Read
    # it through `memory.workspace_settings.effective_principal_role`.
    principal_role: PrincipalRole | None = None
    # The mode resolved for the turn in progress, pinned at its start by
    # `workspace_settings.pin_turn_workspace_mode` (Executive.stream_chat and
    # the committee path) so the tool handlers use the same mode as the
    # persona and tool list. Re-resolved every turn; never an override.
    turn_workspace_mode: str | None = None
    # True for a run nobody is watching that goes through the chat loop (the
    # scheduler's PROACTIVE TRIGGER dispatch). Its prompt quotes stored intent
    # text, so the loop neither offers nor runs the tools in
    # `schedule_tools.UNATTENDED_WITHHELD_TOOLS` — things only the principal
    # decides, such as starting to track a goal.
    unattended: bool = False

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
