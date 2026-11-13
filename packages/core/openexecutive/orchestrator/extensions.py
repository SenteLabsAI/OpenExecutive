"""Chat tools and document collections an install adds without changing this code.

``OPENEXECUTIVE_EXTENSIONS`` names Python modules, comma-separated. Each has a
``register()`` that calls ``register_tool`` and ``register_collection``; they
are imported once, when the API starts (or the first time anything needs them
in a process that has no API, e.g. the CLI).

An extension tool is offered only on a turn someone is in:

- never on an unattended run: the scheduler's proactive trigger is refused
  here, and reflection and research build their toolkits from
  ``_ALL_SKILL_TOOLS``, which never holds these;
- never on a turn private to the principal (mail from one of their contacts);
- never to a speaker who isn't on the People list (mail from a stranger, an
  unknown chat user), so text from outside the team can't reach these tools.
  A tool registered with ``known_caller=False`` is the one exception: it is
  asked on a turn with no verified caller too (a surface that names its user
  without proving who is speaking), and its own ``offered`` decides alone, so
  it must have one that checks who is asking;
- like every tool Act as me's lockdown has not classified, it is refused once
  the turn has read the owner's mail (``delegation.lockdown`` fails closed),
  unless it is registered with ``after_mail_read=True``: a tool that only
  reads the install's own data and sends nothing the model wrote anywhere
  outside, so text in the mail can't use it as a way out (the same holds once
  the turn has read anything from outside the team, ``outside_content``);
- a tool registered with ``reads_outside=True`` returns text from outside the
  team (a vendor app's records): a turn that calls it counts as having read
  outside content, which shuts writes and roster changes for the rest of it;
- and only where the tool's own ``offered(session)`` says so, which should
  depend only on the kind of turn (surface, workspace mode), never on what was
  said, so each kind of turn keeps one cached tool list.

A tool registered with ``workflow=True`` is also offered to workflow action
steps (``workflows/tool_catalog.py``), as ``oe__<name>``: a step names it in
its tool list, which the person approved when they created the workflow. The
step runs with nobody in the turn, so ``offered`` is not asked; while such a
call runs, ``in_workflow_step(name)`` is True for that tool's name only, and
the handler decides for itself what an unattended call may do. A handler
must not call another extension's handler or leave a task running past its
own return (a task copies the flag). ``read_only`` says whether the tool only
reads (the workflow review card's "may change things" badge).

Two answers let such a handler pause the run instead of guessing, and both
stop the step where it is (``workflows/action_step.py``):

- ``workflow_hold(arguments, why)``: this exact call waits for the workflow
  creator's OK. The run pauses after the step; a yes carries the call out (the
  handler runs again inside ``levels.approved()``) and the step carries on
  from there, a no skips it.
- ``workflow_wait(key, what)``: something the tool needs isn't there yet (a
  device that isn't connected). The run pauses, and ``resumer.resume_waiting(key)``
  runs the step again once it is; ``resumer.stop_waiting(key)`` stops it
  there instead, when the person says not to go on (a Stop the tool shows
  them); with no sign of it in time, the run stops and says so. The tool
  that pauses calls one of them when it hears.

A tool may sit in a ``tool_groups`` group (opened with open_tools) instead of
on the direct list, may describe its chip, may name the activity line the chat
shows while it runs, and may keep its dispatch audit row private (``private_row``:
its input or result quotes the speaker's own data). A collection is a kind of
document an extension publishes as a drafted artifact tagged
``collection_tag(name)``: the Documents page gives it its own filter, and the
extension hears when one is archived, restored or deleted, before it happens.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Change = Literal["archived", "restored", "deleted"]

_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# What a workflow step calls an extension tool: the prefix keeps it from ever
# colliding with an MCP name (those are server__tool).
WORKFLOW_PREFIX = "oe__"
_MAX_WORKFLOW_NAME = 64 - len(WORKFLOW_PREFIX)
# The extension tool a workflow step is calling right now, if any.
_in_workflow_step: ContextVar[str | None] = ContextVar("extension_in_workflow_step", default=None)
# A collection or tool group name.
_COLLECTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_COLLECTION_TAG_PREFIX = "collection:"
_MAX_CHIP_SUMMARY = 120


class ExtensionError(ValueError):
    """A registration this module refuses (bad or taken name, bad definition)."""


@dataclass(frozen=True)
class ExtraTool:
    # The Anthropic tool definition: name, description, input_schema.
    definition: dict[str, Any]
    # Runs the call; returns the tool result text (JSON by convention, with
    # an "error" key on failure, like every other tool here).
    handler: Callable[[dict[str, Any]], Awaitable[str]]
    # A tool_groups group to sit in instead of the direct list. A new group
    # needs ``group_purpose`` (what open_tools says it is for).
    group: str | None = None
    group_purpose: str | None = None
    # Whether a turn on this session is offered the tool (see module doc).
    offered: Callable[[Any], bool] | None = None
    # The chip for a successful call: (tool input, parsed JSON result or
    # None) -> {"summary", "target", "link"} or None for no chip. A link must
    # be a path inside the app ("/artifacts/alert:12"). It is asked whatever
    # the result's status, a "waiting_for_approval" one included: what an
    # extension's tool waits on is its own, never core's held action.
    chip: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any] | None] | None = None
    # Also offered to workflow action steps (see module doc), and whether it
    # only reads there (None: may change something).
    workflow: bool = False
    read_only: bool | None = None
    # Still runs once the turn has read the owner's mail (see module doc).
    # Only for a tool that reads this install's own data and carries nothing
    # the model wrote to anywhere outside it; it must also be read_only.
    after_mail_read: bool = False
    # Returns text from outside the team, e.g. a vendor app's records (see
    # module doc and ``orchestrator.outside_content``).
    reads_outside: bool = False
    # A held call can wait on an Approve card and be carried out from it
    # (``orchestrator.levels.carriable``, ``take_the_lead.carry_out``): the
    # handler runs again inside ``levels.approved()`` and must take that tap
    # as the OK it was waiting for.
    card: bool = False
    # Asked on a turn with no verified caller too (see module doc); needs
    # ``offered``.
    known_caller: bool = True
    # Its dispatch audit row is private: its input or result quotes the
    # speaker's own data.
    private_row: bool = False
    # The chat's activity line while it runs ("Checking the draft…").
    activity_label: str | None = None
    # Each call counts toward a workflow step's own call limit, even one a
    # step script makes (scripts otherwise get a larger allowance of their
    # own): for a tool whose every call is slow or acts somewhere, like the
    # browser.
    step_budget: bool = False

    @property
    def name(self) -> str:
        return str(self.definition.get("name", ""))


@dataclass(frozen=True)
class Collection:
    name: str
    # The Documents filter's label, e.g. "Reports".
    label: str
    # Told before one of its documents is archived, restored or deleted:
    # (artifact id, change). If it raises, the change does not happen. It
    # must be idempotent: two requests can report the same change, and the
    # change itself can still fail after it returns.
    on_change: Callable[[str, Change], Awaitable[None]] | None = None


# Built in: loaded whatever ``OPENEXECUTIVE_EXTENSIONS`` says. The browser
# (``openexecutive.browser``) turns itself off with ``OE_BROWSER_OFF=1``.
BUILTIN: tuple[str, ...] = ("openexecutive.browser.tools",)

_tools: dict[str, ExtraTool] = {}
_collections: dict[str, Collection] = {}
_loaded = False
_loading = False
# Reentrant: a register() may read the registry (e.g. get_collection).
_load_lock = threading.RLock()


def _builtin_tool_names() -> frozenset[str]:
    from openexecutive.orchestrator.executive import builtin_tool_names

    return builtin_tool_names()


def register_tool(tool: ExtraTool) -> None:
    """Add a chat tool. Refuses a bad or taken name and a definition without a
    description or an object input schema."""
    name = tool.name
    if not _TOOL_NAME.fullmatch(name):
        raise ExtensionError(f"tool name {name!r} must be lowercase letters, digits and _")
    if name in _tools or name in _builtin_tool_names():
        raise ExtensionError(f"tool name {name!r} is already taken")
    if not str(tool.definition.get("description") or "").strip():
        raise ExtensionError(f"tool {name!r} needs a description")
    schema = tool.definition.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ExtensionError(f"tool {name!r} needs an object input_schema")
    if tool.workflow and len(name) > _MAX_WORKFLOW_NAME:
        raise ExtensionError(f"workflow tool name {name!r} must be at most {_MAX_WORKFLOW_NAME} characters")
    if tool.after_mail_read and tool.read_only is not True:
        raise ExtensionError(f"tool {name!r} may run after a mail read only if it is read_only")
    if not tool.known_caller and tool.offered is None:
        raise ExtensionError(f"tool {name!r} asked without a known caller needs its own offered()")
    if tool.group is not None:
        from openexecutive.orchestrator import tool_groups

        if not _COLLECTION_NAME.fullmatch(tool.group):
            raise ExtensionError(f"group name {tool.group!r} must be lowercase letters, digits and _")

        tool_groups.add_to_group(tool.group, tool.group_purpose, name)
    _tools[name] = tool


def register_collection(collection: Collection) -> None:
    """Add a kind of document with its own Documents filter."""
    if not _COLLECTION_NAME.fullmatch(collection.name):
        raise ExtensionError(
            f"collection name {collection.name!r} must be lowercase letters, digits and _"
        )
    if collection.name in _collections:
        raise ExtensionError(f"collection {collection.name!r} is already registered")
    if not collection.label.strip():
        raise ExtensionError(f"collection {collection.name!r} needs a label")
    _collections[collection.name] = collection


def load() -> None:
    """Import each module ``OPENEXECUTIVE_EXTENSIONS`` names and call its
    ``register()``, once per process. A module that fails is logged and
    skipped, so a bad extension never stops the install from starting."""
    global _loaded, _loading
    if _loaded:
        return
    with _load_lock:
        # A register() that reads the registry calls back in here: it sees
        # what is registered so far rather than loading again.
        if _loaded or _loading:
            return
        _loading = True
        try:
            from openexecutive.config import get_settings

            names = [n for n in (getattr(get_settings(), "extensions", None) or "").split(",") if n]
            names += [n for n in BUILTIN if n not in names]
            for module_name in names:
                tools_before, collections_before = set(_tools), set(_collections)
                try:
                    module = importlib.import_module(module_name)
                    register = getattr(module, "register", None)
                    if not callable(register):
                        raise ExtensionError(f"{module_name} has no register()")
                    register()
                except Exception:
                    logger.exception(
                        "extension %s failed to load; skipped — its tools and "
                        "documents are unavailable until it is fixed",
                        module_name,
                    )
                    # All or nothing: drop what it registered before failing.
                    for name in set(_tools) - tools_before:
                        _forget_tool(name)
                    for name in set(_collections) - collections_before:
                        del _collections[name]
            _loaded = True
        finally:
            _loading = False


def _forget_tool(name: str) -> None:
    tool = _tools.pop(name)
    if tool.group is not None:
        from openexecutive.orchestrator import tool_groups

        tool_groups.remove_from_group(tool.group, name)


def offered_tools(session: Any) -> list[ExtraTool]:
    """The extension tools a turn on ``session`` is offered (the caller has
    already excluded unattended and private turns): unless the speaker is on
    the People list, only those registered ``known_caller=False``; then each
    whose ``offered`` agrees. A failing ``offered`` counts as not offered."""
    known = isinstance(getattr(session, "caller_person_id", None), int)
    load()
    offered: list[ExtraTool] = []
    for tool in _tools.values():
        if not known and tool.known_caller:
            continue
        if tool.offered is not None:
            try:
                if not tool.offered(session):
                    continue
            except Exception:
                logger.exception("extension tool %s: offered() failed", tool.name)
                continue
        offered.append(tool)
    return offered


def allowed_after_mail_read(tool_name: str) -> bool:
    """Whether extension tool ``tool_name`` was registered to run on a turn
    that has read the owner's mail (``after_mail_read``). False for anything
    else, built-in tools included (``delegation.lockdown`` classifies those)."""
    load()
    tool = _tools.get(tool_name)
    return tool is not None and tool.after_mail_read


def reads_outside(tool_name: str) -> bool:
    """Whether extension tool ``tool_name`` was registered as returning text
    from outside the team (``reads_outside``). False for anything else."""
    load()
    tool = _tools.get(tool_name)
    return tool is not None and tool.reads_outside


def private_row(tool_name: str) -> bool:
    """Whether extension tool ``tool_name``'s dispatch audit row is private
    (``private_row``). False for anything else."""
    load()
    tool = _tools.get(tool_name)
    return tool is not None and tool.private_row


def card_handler(tool_name: str) -> Callable[[dict[str, Any]], Awaitable[str]] | None:
    """The handler an Approve card carries out for ``tool_name``: a
    registered tool marked ``card``, else None."""
    load()
    tool = _tools.get(tool_name)
    return tool.handler if tool is not None and tool.card else None


def asks_through_approvals(tool_name: str) -> bool:
    """Whether ``tool_name`` (a workflow tool name, ``oe__`` prefix or not)
    is a registered tool marked ``card``: it puts its own call through
    Approvals and holds on its own Approve card, so a workflow step doesn't
    hold it a second time for writing somewhere new."""
    load()
    tool = _tools.get(tool_name.removeprefix("oe__"))
    return tool is not None and tool.card


def counts_on_step_budget(tool_name: str) -> bool:
    """Whether ``tool_name`` (``oe__`` prefix or not) is marked
    ``step_budget``: a script's call to it counts toward the step's own
    call limit."""
    load()
    tool = _tools.get(tool_name.removeprefix("oe__"))
    return tool is not None and tool.step_budget


def activity_label(tool_name: str) -> str | None:
    """The activity line extension tool ``tool_name`` registered, if any."""
    load()
    tool = _tools.get(tool_name)
    return tool.activity_label if tool is not None else None


def workflow_tools() -> list[ExtraTool]:
    """The extension tools workflow action steps may use (``workflow=True``)."""
    load()
    return [tool for tool in _tools.values() if tool.workflow]


@contextlib.contextmanager
def workflow_step(tool_name: str) -> Iterator[None]:
    """Mark the call inside as a workflow step's call of ``tool_name``."""
    token = _in_workflow_step.set(tool_name)
    try:
        yield
    finally:
        _in_workflow_step.reset(token)


def in_workflow_step(tool_name: str) -> bool:
    """Whether the running call of ``tool_name`` is a workflow action step's:
    one nobody is in, made because the workflow's approved tool list names
    that tool."""
    return _in_workflow_step.get() == tool_name


@dataclass(frozen=True)
class WorkflowSignal:
    """What a workflow step's extension call asked of the run: ``hold`` this
    exact call (``arguments``) for the creator's OK, or ``wait`` until
    ``key`` is ready. ``why`` says it in the creator's words."""

    kind: Literal["hold", "wait"]
    why: str
    arguments: dict[str, Any] | None = None
    key: str = ""


# The signals of the step call running now (``collect_workflow_signals``).
# A list, so what a handler adds inside the call's own task reaches the step.
_signals: ContextVar[list[WorkflowSignal] | None] = ContextVar("extension_workflow_signals", default=None)
# What the model is told after either signal: the run pauses, so it stops.
_STOP = "Stop using tools now and write your report: say what you did and what is left."
_MAX_WHY = 300


@contextlib.contextmanager
def collect_workflow_signals() -> Iterator[list[WorkflowSignal]]:
    """Collect what the workflow step call inside asks of the run."""
    found: list[WorkflowSignal] = []
    token = _signals.set(found)
    try:
        yield found
    finally:
        _signals.reset(token)


def _signal(signal: WorkflowSignal) -> bool:
    found = _signals.get()
    if found is None or _in_workflow_step.get() is None:
        return False
    found.append(signal)
    return True


def workflow_hold(arguments: dict[str, Any], why: str) -> str:
    """Hold this workflow step call for the creator's OK (see module doc):
    ``arguments`` are what runs on a yes, ``why`` what they are asked. Returns
    the tool result for the model. Outside a workflow step, an error."""
    why = " ".join(str(why or "").split())[:_MAX_WHY] or "a step needs their OK"
    if not _signal(WorkflowSignal("hold", why, arguments=dict(arguments or {}))):
        return json.dumps({"error": "Only a workflow step can wait for an OK this way."})
    return json.dumps({
        "status": "waiting_for_approval",
        "message": f"Not done yet: it waits for the workflow creator's OK ({why}). The run carries on from "
        f"here once they say yes. {_STOP}",
    })


def workflow_wait(key: str, what: str) -> str:
    """Pause the run until ``key`` is ready (see module doc): ``what`` names
    it for the creator ("your browser"). Returns the tool result for the
    model. Outside a workflow step, an error."""
    key = str(key or "").strip()[:200]
    what = " ".join(str(what or "").split())[:_MAX_WHY] or "something it needs"
    if not key or not _signal(WorkflowSignal("wait", what, key=key)):
        return json.dumps({"error": f"{what} isn't available right now."})
    return json.dumps({
        "status": "waiting",
        "message": f"{what} isn't available right now. The run pauses and does this step again once it is. "
        f"{_STOP}",
    })


def has_chip(tool_name: str) -> bool:
    tool = _tools.get(tool_name)
    return tool is not None and tool.chip is not None


def chip_fields(
    tool_name: str, tool_input: dict[str, Any], parsed: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The tool's chip fields (summary, target, link), checked: None when it
    has no chip, declines, raises or gives no summary. A link that is not a
    path inside the app is dropped."""
    tool = _tools.get(tool_name)
    if tool is None or tool.chip is None:
        return None
    try:
        raw = tool.chip(tool_input, parsed)
    except Exception:
        logger.exception("extension tool %s: chip() failed", tool_name)
        return None
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    target = raw.get("target")
    link = raw.get("link")
    return {
        "summary": summary.strip()[:_MAX_CHIP_SUMMARY],
        "target": target[:200] if isinstance(target, str) and target else None,
        "link": link if _is_app_path(link) else None,
    }


def _is_app_path(link: Any) -> bool:
    return (
        isinstance(link, str)
        and link.startswith("/")
        and not link.startswith("//")
        and "\\" not in link
        and not any(ch.isspace() or ord(ch) < 32 for ch in link)
        and len(link) <= 500
    )


def collection_tag(name: str) -> str:
    """The topic tag that puts a drafted artifact in collection ``name``."""
    return f"{_COLLECTION_TAG_PREFIX}{name}"


def collection_for_tags(tags: Iterable[str] | None) -> Collection | None:
    """The registered collection a drafted artifact's topic tags name, if any."""
    if not tags:
        return None
    load()
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(_COLLECTION_TAG_PREFIX):
            found = _collections.get(tag[len(_COLLECTION_TAG_PREFIX):])
            if found is not None:
                return found
    return None


def get_collection(name: str | None) -> Collection | None:
    if not name:
        return None
    load()
    return _collections.get(name)


async def notify(collection_name: str | None, artifact_id: str, change: Change) -> None:
    """Tell a document's collection it is about to be archived, restored or
    deleted. Raises whatever the collection raises, so the caller can stop."""
    collection = get_collection(collection_name)
    if collection is not None and collection.on_change is not None:
        await collection.on_change(artifact_id, change)


def _reset_for_tests() -> None:
    """Forget every registration (tests only)."""
    global _loaded, _loading
    for name in list(_tools):
        _forget_tool(name)
    _collections.clear()
    _loaded = _loading = False


__all__ = [
    "Change",
    "Collection",
    "ExtensionError",
    "ExtraTool",
    "WorkflowSignal",
    "activity_label",
    "chip_fields",
    "collect_workflow_signals",
    "collection_for_tags",
    "collection_tag",
    "counts_on_step_budget",
    "get_collection",
    "has_chip",
    "in_workflow_step",
    "load",
    "notify",
    "offered_tools",
    "private_row",
    "register_collection",
    "register_tool",
    "workflow_hold",
    "workflow_step",
    "workflow_tools",
    "workflow_wait",
]
