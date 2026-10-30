"""Chat's tools in workflow action steps: one list, chat's own handlers.

Every tool the chat can offer is labelled here, once (``ACCESS``), and a unit
test fails when a chat tool has no label, so a tool added to the chat is
always decided for workflows too:

- ``USE``: a step may name it as ``oe__<name>`` and it runs through the chat's
  own handler (``tool_catalog`` lists these with the other built-ins).
- ``AS_ME``: reads or writes the person's own mailbox or calendar (Act as
  me). Not offered to workflows yet: it needs its own identity rules.
- ``COVERED``: workflows do this their own way (a specialist step, naming a
  gateway tool directly, the step's own ``run_script``).
- ``CHAT_ONLY``: it needs the person: they are on screen, or it changes what
  the Executive believes or who is on the team, which chat keeps to a turn
  the person is in (most are ``schedule_tools.UNATTENDED_WITHHELD_TOOLS``). A
  workflow reads outside text (mail, pages), and a line in it must not
  quietly rewrite memory with nobody watching. Goal progress and the
  watchlist are the exception, by the owner's choice: keeping them current
  is what a scheduled workflow is for, the step has to be given the tool by
  name when the workflow is saved, and each change is audited.

**How a chat tool runs in a step.** The step's call goes through the usual
checks first (the allowlist, the budget, the first-write target hold in
``action_step``). Then the chat handler runs on a session of its own, as the
chat's unattended runs do (``Session.unattended``: the scheduler's proactive
run, the reflection): the handlers already know that nobody is in the
conversation and refuse what needs someone there. The session's documents
are those of the person the run is for (``artifact_records.current_viewer``),
and a tool the workspace mode doesn't offer is refused
(``filter_tools_for_workspace_mode``). Take the lead does not gate these
calls: a saved workflow is its creator's own instruction, approved tool by
tool when they saved it, as for the gateway tools. A workflow a team member
created goes through their department's approval level, as their own chat
turn would (``department_gate``). One with no creator on record (saved
before creators were) runs as the principal's, as its tools always did.

**Long and nested runs.** ``run_workflow`` and ``run_executive_research``
get a longer timeout than other calls (``TIMEOUT_S``), and a workflow may not
start itself or one already running above it, nor go more than
``MAX_DEPTH`` workflows deep.

**Web search** is the model's own server tool, not a handler: a step that
names ``oe__web_search`` has it added to the model's tools
(``action_step.run_action_step``).
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "oe__"

USE = "use"
AS_ME = "as_me"
COVERED = "covered"
CHAT_ONLY = "chat_only"

WEB_SEARCH = "web_search"

# Every tool the chat can offer: (label, why). Grouped by label.
ACCESS: dict[str, tuple[str, str]] = {
    # USE: runs in a step through the chat's own handler.
    "search_skills": (USE, "find a playbook"),
    "load_skill": (USE, "read a playbook"),
    "search_knowledge": (USE, "search the company knowledge base"),
    "read_document": (USE, "read an uploaded document"),
    WEB_SEARCH: (USE, "search the web (the model's server tool)"),
    "run_executive_research": (USE, "run the research council"),
    "list_workflows": (USE, "list workflows"),
    "run_workflow": (USE, "run another workflow"),
    "run_python_job": (CHAT_ONLY, "reads the principal's Knowledge files; unattended runs never get it"),
    "draft_artifact": (USE, "publish a document, sheet or report"),
    "get_artifact": (USE, "read a past deliverable"),
    "list_artifacts": (USE, "list past deliverables"),
    "create_alert": (USE, "raise an alert"),
    "find_alerts": (USE, "look up alerts"),
    "message_person": (USE, "message someone on the roster"),
    "send_slack_dm": (USE, "Slack direct message"),
    "send_discord_dm": (USE, "Discord direct message"),
    "send_telegram_message": (USE, "Telegram message"),
    "send_department_message": (USE, "post to a department room"),
    "send_company_broadcast": (USE, "post company-wide"),
    "schedule_followup": (USE, "queue a follow-up message"),
    "suggest_workflow": (USE, "suggest a workflow to run"),
    "list_followups": (USE, "list pending follow-ups"),
    "create_calendar_event": (USE, "book a meeting through the meeting gate"),
    "create_instant_meeting": (USE, "start a meeting now"),
    "cancel_calendar_event": (USE, "cancel a meeting it booked"),
    "list_people": (USE, "read the roster"),
    "lookup_person": (USE, "find someone on the roster"),
    "ask_about_person": (USE, "ask what is known about someone"),
    "list_open_loops": (USE, "list open loops"),
    "list_department_goals": (USE, "list goals"),
    "list_watchlist": (USE, "list the watchlist"),
    "add_watchlist_entry": (USE, "watch something new"),
    "tune_watchlist_entry": (USE, "tune what is watched"),
    "remove_watchlist_entry": (USE, "stop watching something"),
    "update_department_goal": (USE, "update a goal's progress"),
    # AS_ME: the person's own mailbox and calendar.
    "ghostwrite_email": (AS_ME, "write an email as the person"),
    "search_my_email": (AS_ME, "search the person's own mailbox"),
    "read_my_email": (AS_ME, "read the person's own mail"),
    "read_my_email_attachment": (AS_ME, "read an attachment in the person's mail"),
    "save_my_email_attachment": (AS_ME, "save an attachment from the person's mail"),
    "my_email_awaiting_reply": (AS_ME, "the person's unanswered sent mail"),
    "my_email_read_before": (AS_ME, "mail opened for the person before"),
    "my_calendar": (AS_ME, "the person's own calendar"),
    "remind_me": (AS_ME, "a reminder for the person"),
    "propose_actions": (AS_ME, "an approval card from the person's mail"),
    "recall_history": (AS_ME, "the person's own conversation notes"),
    "search_past_chats": (AS_ME, "the person's own earlier chats"),
    # COVERED: workflows have their own way.
    "consult_specialist": (COVERED, "a specialist step"),
    "search_tools": (COVERED, "a step names gateway tools directly"),
    "call_tool": (COVERED, "a step names gateway tools directly"),
    "load_mcp_server": (COVERED, "connections are set up outside a run"),
    "run_script": (COVERED, "every action step has run_script"),
    "list_saved_tools": (COVERED, "every action step's script lists them"),
    "open_tools": (COVERED, "a step names its tools up front"),
    "use_tool": (COVERED, "a step names its tools up front"),
    # CHAT_ONLY: needs the person (module docstring).
    "remember_fact": (CHAT_ONLY, "changes what the Executive takes as fact"),
    "forget_fact": (CHAT_ONLY, "changes what the Executive takes as fact"),
    "update_company_profile": (CHAT_ONLY, "changes the company profile"),
    "create_goal": (CHAT_ONLY, "only the principal starts a goal"),
    "record_decision_outcome": (CHAT_ONLY, "only the principal records an outcome"),
    "assign_open_loop": (CHAT_ONLY, "takes the task in the speaker's own words"),
    "close_open_loop": (CHAT_ONLY, "only the principal or the loop's owner closes it"),
    "ack_alert": (CHAT_ONLY, "clears an alert on quoted text"),
    "leave_out_of_brief": (CHAT_ONLY, "a correction the person asks for"),
    "cancel_followup": (CHAT_ONLY, "a correction the person asks for"),
    "move_followup": (CHAT_ONLY, "a correction the person asks for"),
    "upsert_person": (CHAT_ONLY, "changes the roster"),
    "archive_person": (CHAT_ONLY, "changes the roster"),
    "set_department_head": (CHAT_ONLY, "changes the roster"),
    "resolve_roster_request": (CHAT_ONLY, "answers a roster request"),
    "do_in_app": (CHAT_ONLY, "acts in the app as the person"),
    "find_in_app": (CHAT_ONLY, "acts in the app as the person"),
    "propose_form_values": (CHAT_ONLY, "fills a form on the person's screen"),
    "open_in_app": (CHAT_ONLY, "a link for the person on screen"),
    "read_user_guide": (CHAT_ONLY, "explains the app to the person"),
    "mark_draft_sent": (CHAT_ONLY, "the person says they sent it"),
    "draft_workflow": (CHAT_ONLY, "a workflow does not build workflows"),
    "save_workflow": (CHAT_ONLY, "a workflow does not build workflows"),
}

# Calls that run a whole job: longer than action_step's default timeout.
# Workflow tool names saved workflows may still list, and the chat tool each
# now runs. Not offered for new steps.
LEGACY_NAMES: dict[str, str] = {"oe__read_file": "read_document"}

TIMEOUT_S: dict[str, float] = {"run_workflow": 900.0, "run_executive_research": 900.0}
MAX_DEPTH = 3


def chat_tool_definitions() -> dict[str, dict[str, Any]]:
    """Every tool the chat can offer, by name (definitions as chat offers
    them; web search is the model's server tool and has none here)."""
    from openexecutive.orchestrator import tool_groups
    from openexecutive.orchestrator.chat_search_tools import CHAT_SEARCH_TOOLS
    from openexecutive.orchestrator.delegation_tools import DELEGATION_TOOLS
    from openexecutive.orchestrator.executive import _offered_skill_tools
    from openexecutive.orchestrator.history_tools import HISTORY_TOOLS
    from openexecutive.orchestrator.knowledge_tools import KNOWLEDGE_TOOLS
    from openexecutive.orchestrator.mcp_gateway import MCP_TOOLS
    from openexecutive.orchestrator.router import SPECIALIST_TOOLS
    from openexecutive.workflows import step_script

    tools = [
        *SPECIALIST_TOOLS, *_offered_skill_tools(), *MCP_TOOLS, *DELEGATION_TOOLS,
        *HISTORY_TOOLS, *CHAT_SEARCH_TOOLS, *KNOWLEDGE_TOOLS, tool_groups.USE_TOOL_TOOL,
        {"name": tool_groups.OPEN_TOOLS},
        {"name": step_script.RUN_SCRIPT_TOOL}, {"name": step_script.LIST_SAVED_TOOLS_TOOL},
    ]
    return {str(t["name"]): t for t in tools}


def _chat_handlers() -> dict[str, Callable[[dict[str, Any]], Awaitable[str]]]:
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS
    from openexecutive.orchestrator.knowledge_tools import KNOWLEDGE_TOOL_HANDLERS

    return {**_ALL_SKILL_HANDLERS, **KNOWLEDGE_TOOL_HANDLERS}


def label(name: str) -> str | None:
    entry = ACCESS.get(name)
    return entry[0] if entry else None


def workflow_name_for(chat_name: str) -> str:
    return f"{PREFIX}{chat_name}"


# The workflows running in this task, outermost first, and the creator of
# the innermost one (set by ``action_step._dispatch``).
_chain: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("workflow_chain", default=())
_creator: contextvars.ContextVar[int | None] = contextvars.ContextVar("workflow_tool_creator", default=None)
# Whether the outermost of them was started by one of the Executive's own
# unattended passes (the reflection, research, a proactive run): their
# context is stored or inbound text with nobody watching.
_by_a_pass: contextvars.ContextVar[bool] = contextvars.ContextVar("workflow_started_by_a_pass", default=False)


@contextlib.contextmanager
def in_workflow(workflow_name: str, creator_id: int | None) -> Iterator[None]:
    """Run the block as a step of ``workflow_name``, created by ``creator_id``."""
    from openexecutive.orchestrator.schedule_tools import current_session

    outermost = not _chain.get()
    by_a_pass = _by_a_pass.set(
        bool(getattr(current_session.get(), "unattended", False)) if outermost else _by_a_pass.get()
    )
    chain = _chain.set((*_chain.get(), workflow_name))
    creator = _creator.set(creator_id)
    try:
        yield
    finally:
        _creator.reset(creator)
        _chain.reset(chain)
        _by_a_pass.reset(by_a_pass)


def running() -> tuple[tuple[str, ...], int | None]:
    """The workflows running in this task, outermost first, and the creator
    of the innermost one (``in_workflow``)."""
    return _chain.get(), _creator.get()


def started_by_a_pass() -> bool:
    """Whether the outermost running workflow was started by one of the
    Executive's unattended passes rather than a person or its schedule."""
    return _by_a_pass.get()


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _nested_run_refusal(tool_input: dict[str, Any]) -> str | None:
    chain = _chain.get()
    target = str(tool_input.get("name") or tool_input.get("workflow") or "").strip()
    if target and target in chain:
        return _error(f"{target!r} is already running in this chain of workflows, so it can't start again. Do not retry.")
    if len(chain) >= MAX_DEPTH:
        return _error(f"Workflows can start other workflows at most {MAX_DEPTH} deep. Do not retry.")
    return None


def _seen_channel_refs() -> set[tuple[str, str]]:
    """The roster's own channels, as the reflection seeds them, so
    ``schedule_followup`` reaches only a real person's channel."""
    from openexecutive.people.store import list_people

    seen: set[tuple[str, str]] = set()
    for person in list_people():
        if person.slack_user_id:
            seen.add(("slack_dm", person.slack_user_id))
        if person.discord_user_id:
            seen.add(("discord_dm", person.discord_user_id))
        if person.telegram_chat_id:
            seen.add(("telegram", person.telegram_chat_id))
        if person.email:
            seen.add(("email", person.email))
    return seen


def _session(mode: str) -> Any:
    from openexecutive.memory.workspace_settings import effective_principal_role
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.artifact_records import current_viewer
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    return Session(
        company_profile=profile if not profile.is_empty() else None,
        seen_channel_refs=_seen_channel_refs(),
        turn_workspace_mode=mode,
        turn_principal_role=effective_principal_role(None),
        unattended=True,
        documents_viewer=current_viewer(),
    )


def _team_member(person_id: int | None) -> Any:
    """The creator when they are a team member other than the principal."""
    from openexecutive.people.store import get_person

    if person_id is None:
        return None
    person = get_person(person_id)
    if person is None or person.is_principal or person.kind != "team":
        return None
    return person


async def run_chat_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Run the chat tool ``name`` for a workflow step (module docstring)."""
    from openexecutive.memory.workspace_settings import effective_workspace_mode
    from openexecutive.orchestrator import department_gate
    from openexecutive.orchestrator.schedule_tools import (
        current_session,
        filter_tools_for_workspace_mode,
        withheld_tool_error,
    )

    if label(name) != USE:
        return _error(f"{name} is not available in workflows. Do not retry.")
    handler = _chat_handlers().get(name)
    if handler is None:
        return _error(f"{name} is not available in workflows. Do not retry.")
    if name == "run_workflow":
        refused = _nested_run_refusal(tool_input)
        if refused is not None:
            return refused
    mode = str(effective_workspace_mode(None))
    if not filter_tools_for_workspace_mode([{"name": name}], mode):
        return withheld_tool_error(name, mode)
    session = _session(mode)
    handlers: dict[str, Any] = {name: handler}
    seat = _team_member(_creator.get())
    if seat is not None:
        handlers = department_gate.gated_handlers(handlers, seat, session_id=session.session_id)
    token = current_session.set(session)
    try:
        return str(await handlers[name](tool_input))
    finally:
        current_session.reset(token)


def _usable_definitions() -> list[dict[str, Any]]:
    definitions = chat_tool_definitions()
    return [definitions[n] for n, (lbl, _) in ACCESS.items() if lbl == USE and n in definitions]


def not_offered() -> set[str]:
    """Workflow tool names that resolve but aren't shown for new steps: the
    old names, and channel tools this instance has no token for (a call
    would only error)."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.schedule_tools import filter_tools_for_configured_channels

    usable = _usable_definitions()
    try:
        kept = {t["name"] for t in filter_tools_for_configured_channels(usable, get_settings())}
    except Exception:
        logger.warning("workflow chat tools: couldn't read the configured channels", exc_info=True)
        kept = {t["name"] for t in usable}
    dropped = {workflow_name_for(str(t["name"])) for t in usable if t["name"] not in kept}
    return set(LEGACY_NAMES) | dropped


def workflow_specs() -> dict[str, tuple[Any, Callable[[dict[str, Any]], Awaitable[str]]]]:
    """``{oe__name: (ToolInfo, handler)}`` for every ``USE`` chat tool, plus
    the old names in ``LEGACY_NAMES``. All of them resolve, so a saved
    workflow keeps validating; ``not_offered`` says which to leave out of
    what new steps are shown."""
    from openexecutive.workflows.tool_catalog import ToolInfo, read_only_label

    usable = _usable_definitions()
    specs: dict[str, tuple[Any, Callable[[dict[str, Any]], Awaitable[str]]]] = {}
    for tool in usable:
        chat_name = str(tool["name"])
        schema = dict(tool.get("input_schema") or {"type": "object", "properties": {}})

        async def run(tool_input: dict[str, Any], _name: str = chat_name) -> str:
            return await run_chat_tool(_name, tool_input)

        specs[workflow_name_for(chat_name)] = (
            ToolInfo(
                name=workflow_name_for(chat_name),
                description=str(tool.get("description") or ""),
                input_schema=schema,
                read_only=read_only_label(chat_name, schema),
                source="builtin",
                timeout_s=TIMEOUT_S.get(chat_name),
            ),
            run,
        )
    for legacy, chat_name in LEGACY_NAMES.items():
        if workflow_name_for(chat_name) in specs:
            info, handler = specs[workflow_name_for(chat_name)]
            specs[legacy] = (dataclasses.replace(info, name=legacy), handler)
    from openexecutive.orchestrator.web_search_tool import build_web_search_tool

    if build_web_search_tool() is not None:
        specs[workflow_name_for(WEB_SEARCH)] = (
            ToolInfo(
                name=workflow_name_for(WEB_SEARCH),
                description=(
                    "Search the web for current facts, news, prices and sources. The "
                    "model searches as it writes; there is nothing to call."
                ),
                input_schema={"type": "object", "properties": {}},
                read_only=None,
                source="server",
            ),
            _web_search_not_callable,
        )
    return specs


async def _web_search_not_callable(tool_input: dict[str, Any]) -> str:
    return _error("Web search runs as the model writes; it can't be called from a script.")
