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
    # Which inbound channel this session arrived on ("slack", "discord",
    # "telegram", "google_chat", "email"), and the address on it. Empty for
    # web/CLI turns and runs nobody started from a channel. Used when a
    # workflow raises an approval gate mid-conversation: the gate records
    # where to look for the answer, so a reply on this channel can resolve it
    # (not on email, whose reply is no chat message — see `gate_delivery`).
    # Also how `content_trust.principal_speaking` tells an inbound email from
    # the principal's own surfaces: an email turn that left it empty was read
    # as the web app's. Inbound vocabulary — see `normalize_channel`.
    origin_channel: str = ""
    origin_channel_ref: str = ""
    # True only for a session minted by the web chat route. `origin_channel`
    # cannot stand in for this: alert review, the CLI, the MCP server, the
    # scheduler and the unattended workflows all leave it empty while being
    # nothing like a browser turn. Anything that wants to treat browser turns
    # differently has to ask for them by name.
    from_web_chat: bool = False
    # True only for a session minted by the CLI (`openexecutive ask` / `chat`),
    # which runs on the host itself — the principal's own surface for
    # `content_trust.principal_speaking`, as the web chat is.
    from_cli: bool = False
    # The rostered Person behind this conversation, when one is resolved. The
    # adapters already pass this to `Executive.chat(person_id=...)`; holding it
    # on the session too lets tool handlers running mid-turn tell "the approver
    # is the person I'm already talking to" from "the approver is someone else".
    caller_person_id: int | None = None
    # Set by a channel adapter that verified for itself that `caller_person_id`
    # is who sent this turn's message, on a channel the checks in
    # `people_tools` don't know. Read only by Always in the loop
    # (`memory.history_chat`, `orchestrator.history_tools`), never as a grant
    # for anything else. `private_chat` adds that nobody but that person (and
    # the Executive) can read the conversation, so their notes may be recalled.
    # An adapter sets either only when it resolved `caller_person_id` from the
    # sender identity it verified itself, never from anything the message says.
    speaker_verified: bool = False
    private_chat: bool = False
    # The live alert board as the server derived it this turn, recorded by
    # `briefing.context.render_and_trust`. `ack_alert` refuses anything else on
    # EVERY session, web included, so an id quoted inside an alert's own body —
    # alerts are minted from inbound mail and chat, so that text is
    # attacker-controlled — cannot clear a row that is closed, snoozed or
    # invented. It does not stop the model being argued into acking the wrong
    # LIVE card; see `format_open_alerts_for_prompt` for the limits of this
    # control. Empty means the turn was shown no board and can ack nothing.
    # `find_alerts` is the one thing that adds to it after that, and only on a
    # turn with `principal_board_shown` — so an open row off the board (read,
    # snoozed, past TTL but not yet swept) can be cleared there too.
    trusted_alert_ids: set[int] = field(default_factory=set)
    # Whether `render_and_trust` showed THIS turn the principal's own board:
    # the principal on a verified surface, and on a channel only in their DM
    # (`channel_context.attach_briefing_context`). `find_alerts` answers and
    # widens nothing without it — "principal on a verified surface" alone
    # also passes the principal's turn in a shared Slack or Discord thread.
    principal_board_shown: bool = False
    # Ids `find_alerts` added to `trusted_alert_ids` this turn, capped across
    # calls (`schedule_tools._FIND_ALERTS_MAX_PER_TURN`). Reset per turn with
    # the trusted set.
    found_alert_ids: set[int] = field(default_factory=set)
    # Pending roster requests ("who is this new sender?") the server showed
    # this turn in its <roster_requests> block — the principal's own verified
    # turn only (`briefing.context.render_and_trust`). `resolve_roster_request`
    # answers nothing else, so an id the model invents or reads out of text
    # cannot add anyone.
    trusted_roster_request_ids: set[int] = field(default_factory=set)
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
    # Per-session voice persona slug (None = the Executive override's voice,
    # else Direct), so an eval scenario can play a voice without writing the
    # install-wide override that concurrent scenarios share.
    voice_persona_slug: str | None = None
    # The mode resolved for the turn in progress, pinned at its start by
    # `workspace_settings.pin_turn_workspace_mode` (Executive.stream_chat and
    # the committee path) so the tool handlers use the same mode as the
    # persona and tool list. Re-resolved every turn; never an override.
    turn_workspace_mode: str | None = None
    # The principal's role resolved for the turn in progress, pinned with the
    # mode by `workspace_settings.pin_turn_principal_role` (an empty role in
    # team), so the org block, the specialists' <principal_role> tag and any
    # workflow the turn starts agree even if the role is edited mid-turn.
    # Re-resolved every turn; never an override.
    turn_principal_role: PrincipalRole | None = None
    # True for a run nobody is watching that goes through the chat loop (the
    # scheduler's PROACTIVE TRIGGER dispatch). Its prompt quotes stored intent
    # text, so the loop neither offers nor runs the tools in
    # `schedule_tools.UNATTENDED_WITHHELD_TOOLS` — things only the principal
    # decides, such as starting to track a goal.
    unattended: bool = False
    # True for a turn about something private to the principal (mail from one
    # of their contacts, mail they forwarded): an alert raised on it is
    # private to the principal (``alerts.models.PRIVATE_ALERT_TAG``) and it
    # may not draft a team-visible artifact. Set by the email poller.
    private_to_principal: bool = False
    # The bare, lowercased From address of the inbound email this turn is
    # answering, and whether it is the principal's primary address with
    # Gmail's own Authentication-Results reporting dmarc=pass for it
    # (``integrations.fact_confirmation.authenticated_by_gmail``; fails
    # closed). Set by the email poller only; empty / False on every other
    # surface. The fact tools read them to let the principal's own
    # email request a standing fact, held until a token reply confirms it
    # (``orchestrator.fact_tools``) — a From line alone proves nothing.
    email_from: str = ""
    email_authenticated: bool = False
    # Act as me for the turn in progress (``delegation.settings.TurnDelegation``),
    # pinned at its start by ``pin_turn_delegation``: whether the speaker has
    # it on, whether ``ghostwrite_email`` is offered, and whether the turn has
    # touched their mailbox (every audit row it writes after that is private).
    # Re-resolved every turn; never an override.
    turn_delegation: Any = None
    # True when this web request carried a signed-in caller (``x-caller-email``,
    # stamped by the UI proxy from the sign-in), set per turn by the chat
    # route. A header-less request resolves to the principal but is no
    # sign-in: Act as me needs one (or local login).
    web_caller_signed_in: bool = False
    # Whose documents this session reads and publishes, when that is not its
    # own speaker (``artifact_records.current_viewer``): a session a workflow
    # mints inside someone's turn (``executive_research``'s synthesis) is
    # pinned to that turn's viewer, so what it drafts is theirs. An
    # ``artifact_records.Viewer``; None derives it from this session.
    documents_viewer: Any = None
    # Evals and tests only (``delegation.settings.DelegationOverride``): run
    # as if the speaker had Act as me, against a fake mailbox.
    delegation_override: Any = None

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
