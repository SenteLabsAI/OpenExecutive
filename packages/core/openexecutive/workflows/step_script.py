"""Let a workflow action step act through one short script instead of many turns.

An action step's agent normally calls its tools one model turn at a time, so
"go through this Drive folder and file each document" costs a turn — tokens
and seconds — per file. ``run_script`` lets the agent write that loop once, in
Python, and get back only what the script returns.

The script runs in Monty (``pydantic-monty``), a sandboxed Python interpreter
for code a model wrote, in a worker process spawned per script (so no state
survives from one script to the next):

* **Nothing but the step's own tools.** A call to any function the script did
  not define pauses the worker and comes back here, where it is answered
  through the action step's own per-call path (``action_step._StepCalls.call``):
  the allowlist (a name outside it is refused and audited, like a direct
  ``tool_use``), the call budget, the first-write target check (a held write
  comes back to the script as the usual ``{"status": "held", …}`` result) and
  the audit row. A script is a faster way to make the same calls, never a way
  around them. There are no files, network, environment variables,
  subprocesses or third-party imports; OS calls get Monty's refusal.
* **Driven from the event loop.** The worker is stepped with ``feed_start`` /
  ``resume``, so each call is awaited in the step's own task: cancelling the
  run, or the wall clock below, stops the script and its in-flight call
  together, and the engine sees each call's events as it happens.
* **Bounded.** Execution time (not counting time waiting on tools), memory and
  recursion are capped inside the worker, the whole script has a wall-clock
  limit, and the script's text and printed output are size-capped (printing
  past the cap is dropped, not fatal).
* **Not stored.** The script lives only in this step's model turns, like any
  other tool call's arguments; a workflow definition still holds no code.

Monty supports a subset of Python (no classes with inheritance, no
third-party packages), which is enough for loops, conditions, string and
data handling over tool results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

RUN_SCRIPT_TOOL = "run_script"

MAX_SCRIPT_CHARS = 20_000
_MAX_PRINTED_CHARS = 8_000
# Calls listed back to the model when a script fails partway (so it does not
# repeat writes that already ran).
_MAX_LISTED_CALLS = 200
# Worker-enforced limits. Execution time excludes time spent waiting on the
# host (tool calls). The number of tool calls is bounded by the step's own
# budget (``_StepCalls``); `max_suspensions` is only a backstop well above any
# step's budget, since name lookups and OS calls count towards it too.
_LIMITS = {
    "max_feed_duration_secs": 20.0,
    "max_memory": 128 * 1024 * 1024,
    "max_recursion_depth": 200,
    "max_suspensions": 5_000,
}
_WALL_CLOCK_S = 600.0
_POOL_REQUEST_TIMEOUT_S = 180.0

# Statuses a call answers with when it is held for a person's approval
# (action_step.HELD_TOOL_RESULT, take_the_lead's gate).
_WAITING = frozenset({"held", "waiting_for_approval"})

CallFn = Callable[[str, dict[str, Any]], Coroutine[Any, Any, tuple[str, bool]]]
# What ``run_script`` yields: ("call", None) after each tool call (so the
# caller can hand that call's events to the engine), then exactly one
# ("done", (result text, is_error)).
ScriptYield = tuple[str, Any]

_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def function_names(tools: list[str]) -> dict[str, str]:
    """Python function name → tool name, for the tools that map cleanly.

    MCP names (``server__tool``) and built-ins (``oe__…``) are identifiers
    already; one with a hyphen gets underscores. Two tools that would share a
    function name get none — the script reaches them with ``call_tool``.
    """
    mapped: dict[str, list[str]] = {}
    for tool in tools:
        fn = _IDENT_RE.sub("_", tool)
        if fn[:1].isdigit():
            fn = f"t_{fn}"
        mapped.setdefault(fn, []).append(tool)
    return {fn: names[0] for fn, names in mapped.items() if len(names) == 1 and fn != "call_tool"}


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "Python source. End with an expression whose value you want back.",
        },
        "save_as": {
            "type": "string",
            "description": "Optional, with script: keep it as a saved tool under this snake_case name if the run succeeds.",
        },
        "description": {
            "type": "string",
            "description": "With save_as: one sentence on what the tool does and which inputs it takes.",
        },
        "tool": {
            "type": "string",
            "description": "Instead of script: the name of a saved tool to run.",
        },
        "inputs": {
            "type": "object",
            "description": "Values for this run, read by the script as the dict `inputs`.",
        },
    },
}
# A workflow step runs saved tools but never saves one (see run_script_tool).
_STEP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        k: v for k, v in _INPUT_SCHEMA["properties"].items() if k not in ("save_as", "description")
    },
}
_CALL_TEXT = (
    "A call returns the tool's result, parsed from JSON when it is JSON (else "
    "the text); a failed or refused call raises RuntimeError with the reason. "
    'A call that comes back {"status": "held", ...} or {"status": '
    '"waiting_for_approval", ...} is waiting for a person: do not retry it, '
    "stop the loop if the rest depends on it, and say what is waiting."
)
_SAVED_TEXT = (
    "To keep a script that worked for next time, also pass save_as (a "
    "snake_case name) and description (one sentence on what it does and which "
    "inputs it takes); it is saved only if this run succeeds, and only on the "
    "principal's own turns. Write a script "
    "you mean to reuse so it reads its per-run values (a folder, a date) from "
    "the dict `inputs` rather than hard-coding them. Saving again under the same "
    "name keeps a new version. To run a saved tool, pass tool (its name) and "
    "inputs instead of script."
)
_SANDBOX_TEXT = (
    "The script runs in a sandbox with a subset of Python: no imports beyond "
    "json, re, math, datetime, collections, itertools and similar standard "
    "modules; no files, network or classes with inheritance. The value of the "
    "last expression is returned to you, along with anything printed and the "
    "calls the script made. If it fails partway, the calls that already ran are "
    "listed: do not repeat them."
)


def tool_definition(
    tools: list[str], saved: list[Any] | None = None, *, call_budget: int | None = None
) -> dict[str, Any]:
    """The ``run_script`` tool for a workflow step with these tools.

    ``saved`` is the saved tools this step may run (every tool each uses is
    one of the step's), listed by name and description. ``call_budget`` is
    how many calls this step's scripts may make in all.
    """
    budget_text = (
        f"This step's scripts may make up to {call_budget} tool calls in all, a "
        "budget of their own (larger than the direct-call budget, because a "
        "script's calls cost no model turn); "
        if call_budget is not None
        else "Every call counts against this step's tool budget and "
    )
    funcs = function_names(tools)
    listing = "\n".join(f"- {fn}(...)  # calls {name}" for fn, name in sorted(funcs.items()))
    saved_listing = ""
    if saved:
        saved_listing = (
            "\n\nSaved tools this step can run: pass tool=<name> and inputs={...} "
            "instead of script.\n"
        ) + "\n".join(
            f"- {t.name}: {t.description}" for t in saved
        )
    return {
        "name": RUN_SCRIPT_TOOL,
        "description": (
            "Run a short Python script that calls this step's tools, when the work "
            "repeats over many items (every file in a folder, every row, every "
            "email) or chains calls with simple logic. One script replaces many "
            "separate tool calls; use the tools directly for a call or two.\n\n"
            "Each tool is a function taking the tool's arguments as keywords:\n"
            f"{listing}\n"
            "call_tool(name, arguments) also reaches any of them by exact name. "
            f"{_CALL_TEXT} {budget_text}each call "
            "follows the same rules as calling the tool directly.\n\n"
            f"{_SANDBOX_TEXT}{saved_listing}"
        ),
        "input_schema": _STEP_INPUT_SCHEMA,
    }


# The chat version is a constant: chat's tool list is cached, so it can't
# name the tools a conversation happens to use.
CHAT_TOOL_DEFINITION: dict[str, Any] = {
    "name": RUN_SCRIPT_TOOL,
    "description": (
        "Run a short Python script that calls external tools through the tool "
        "gateway, when the work repeats over many items (every file in a folder, "
        "every row, every email) or chains calls with simple logic. One script "
        "replaces many separate call_tool uses; for a call or two, use call_tool "
        "directly.\n\n"
        "Call a tool as a function named exactly like it, with its arguments as "
        "keywords (google_workspace__list_drive_items(folder_id=...)), or "
        "call_tool(name, arguments) by exact name (needed for a name with a "
        "hyphen). Only tools the gateway would let call_tool reach (ones "
        "search_tools has returned) can be called, and every call is checked and "
        f"recorded exactly as a call_tool would be. {_CALL_TEXT}\n\n"
        f"{_SANDBOX_TEXT}\n\n{_SAVED_TEXT} list_saved_tools shows the saved tools."
    ),
    "input_schema": _INPUT_SCHEMA,
}


def _parse_result(content: str) -> Any:
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return content


class _Printed:
    """Collects the script's printed output up to a cap, then drops the rest."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.dropped = False

    def __call__(self, stream: str, text: str) -> None:
        room = _MAX_PRINTED_CHARS - self.size
        if room <= 0:
            self.dropped = True
            return
        piece = text[:room]
        self.parts.append(piece)
        self.size += len(piece)
        if len(piece) < len(text):
            self.dropped = True

    def text(self) -> str:
        out = "".join(self.parts)
        return out + "\n…[more output not shown]" if self.dropped else out


def _call_arguments(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(tool name, arguments) for a call the script made to ``name``.

    ``call_tool(name, arguments=None, **more)`` names the tool itself; any other
    function takes the tool's arguments as keywords, or one positional dict.
    Raises TypeError for a call shape the tool can't take.
    """
    if name == "call_tool":
        if not args or not isinstance(args[0], str):
            raise TypeError("call_tool(name, arguments): name must be a string")
        extra = args[1] if len(args) > 1 else kwargs.pop("arguments", None)
        if len(args) > 2 or (extra is not None and not isinstance(extra, dict)):
            raise TypeError("call_tool(name, arguments): arguments must be a dict")
        return args[0], {**(extra or {}), **kwargs}
    if args:
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            return name, dict(args[0])
        raise TypeError(f"{name}: pass the tool's arguments by name")
    return name, dict(kwargs)


# A gateway tool name (server__tool) used as a function in a chat script:
# non-empty on both sides of `__`, so dunders and stray underscores never
# become gateway calls.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*__[A-Za-z0-9][A-Za-z0-9_]*$")


async def run_script(
    script: str,
    tools: list[str] | None,
    call: CallFn,
    *,
    wall_clock_s: float = _WALL_CLOCK_S,
    inputs: dict[str, Any] | None = None,
) -> AsyncGenerator[ScriptYield, None]:
    """Run a script, yielding after each tool call and then its result.

    ``tools`` is a workflow step's allowlist: its tools are the script's
    functions. ``None`` (chat) makes any tool-shaped function name a call by
    that exact name; ``call`` decides what may run. Never raises (cancellation
    aside, which stops the worker with it).
    """
    from pydantic_monty import (
        AsyncFunctionSnapshot,
        AsyncMonty,
        AsyncNameLookupSnapshot,
        MontyComplete,
        MontyError,
    )

    if not script.strip():
        yield ("done", (json.dumps({"error": "the script is empty"}), True))
        return
    if len(script) > MAX_SCRIPT_CHARS:
        yield ("done", (
            json.dumps({"error": f"the script is longer than {MAX_SCRIPT_CHARS} characters"}), True
        ))
        return

    names = function_names(tools) if tools is not None else {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wall_clock_s

    async def bounded(awaitable: Any) -> Any:
        # Not asyncio.timeout: a consumer may drive this generator one step
        # per task (the chat route does), and a timeout scope only cancels the
        # task that entered it. Each await gets what is left instead.
        left = deadline - loop.time()
        if left <= 0:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise TimeoutError
        return await asyncio.wait_for(awaitable, timeout=left)
    printed = _Printed()
    made: list[dict[str, Any]] = []

    def payload(**fields: Any) -> str:
        if made:
            fields["calls"] = made[:_MAX_LISTED_CALLS]
            if len(made) > _MAX_LISTED_CALLS:
                fields["calls_not_listed"] = len(made) - _MAX_LISTED_CALLS
        if printed.size or printed.dropped:
            fields["printed"] = printed.text()
        return json.dumps(fields, default=str, ensure_ascii=False)

    try:
        async with (
            AsyncMonty(
                min_processes=1, max_processes=1, request_timeout=_POOL_REQUEST_TIMEOUT_S
            ) as pool,
            pool.checkout(
                script_name="step_script.py",
                limits=_LIMITS,  # type: ignore[arg-type]
                os_policy={"sleep": "zero"},
            ) as session,
        ):
            snapshot: Any = await bounded(
                session.feed_start(script, inputs={"inputs": dict(inputs or {})}, print_callback=printed)
            )
            while not isinstance(snapshot, MontyComplete):
                if isinstance(snapshot, AsyncFunctionSnapshot):
                    if snapshot.is_os_function:
                        # open(), os.environ … — Monty's own refusal.
                        snapshot = await bounded(snapshot.resume_not_handled())
                        continue
                    fn = str(snapshot.function_name)
                    if (
                        tools is None
                        and fn != "call_tool"
                        and len(fn) <= 64
                        and _TOOL_NAME_RE.match(fn)
                    ):
                        names[fn] = fn  # chat: an MCP-shaped name is the tool's own name
                    if fn != "call_tool" and fn not in names:
                        snapshot = await bounded(snapshot.resume(
                            {"exception": NameError(f"name {fn!r} is not defined")}
                        ))
                        continue
                    try:
                        tool, arguments = _call_arguments(
                            names.get(fn, fn), tuple(snapshot.args), dict(snapshot.kwargs)
                        )
                    except TypeError as exc:
                        snapshot = await bounded(snapshot.resume({"exception": exc}))
                        continue
                    content, is_error = await bounded(call(tool, arguments))
                    entry: dict[str, Any] = {"tool": tool, "ok": not is_error}
                    parsed = _parse_result(content)
                    if isinstance(parsed, dict) and parsed.get("status") in _WAITING:
                        entry["waiting_for_approval"] = True
                    made.append(entry)
                    yield ("call", None)
                    if is_error:
                        snapshot = await bounded(snapshot.resume(
                            {"exception": RuntimeError(f"{tool} failed: {content}")}
                        ))
                    else:
                        snapshot = await bounded(snapshot.resume({"return_value": parsed}))
                elif isinstance(snapshot, AsyncNameLookupSnapshot):
                    # An undefined name used as a value: leave it undefined.
                    snapshot = await bounded(snapshot.resume())
                else:
                    # A future: the script awaited something it started
                    # without awaiting. Tools are plain calls here.
                    raise MontyScriptShape()
            result = snapshot.output
    except TimeoutError:
        yield ("done", (payload(error="the script ran past its time limit"), True))
        return
    except MontyScriptShape:
        yield ("done", (payload(error="call tools as plain functions, without async or await"), True))
        return
    except MontyError as exc:
        # The script's own traceback, so the model can fix it. It names only
        # the script's code and values it handled — data the model already saw.
        try:
            display = getattr(exc, "display", None)
            detail = str(display()) if callable(display) else f"{type(exc).__name__}: {exc}"
        except Exception:  # noqa: BLE001
            detail = type(exc).__name__
        yield ("done", (payload(error="the script failed", detail=detail[-4_000:]), True))
        return
    except Exception as exc:
        logger.warning("step script: runner failed (%s)", type(exc).__name__)
        yield ("done", (payload(error="the script could not be run"), True))
        return
    yield ("done", (payload(result=result), False))


LIST_SAVED_TOOLS_TOOL = "list_saved_tools"
# Chat's view of the saved tools (constant definition, so the cached tool
# prefix is stable). Workflow steps get theirs in run_script's description.
LIST_SAVED_TOOLS_DEFINITION: dict[str, Any] = {
    "name": LIST_SAVED_TOOLS_TOOL,
    "description": (
        "List the saved tools: scripts kept earlier with run_script(save_as=...) "
        "that run_script(tool=<name>, inputs={...}) can run again. Each entry has "
        "its name, what it does and the tools it calls. Check here before writing "
        "a script for a job you have done before."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

_MAX_INPUTS_CHARS = 20_000
_MAX_LISTED_SAVED_TOOLS = 30


def list_saved_tools_result() -> str:
    """The list_saved_tools answer: the enabled saved tools, never their scripts."""
    from openexecutive.config import get_settings
    from openexecutive.workflows import saved_tools

    if not get_settings().saved_tools_enabled:
        return json.dumps({"saved_tools": [], "note": "saved tools are turned off"})
    try:
        listed = saved_tools.list_tools(enabled_only=True)
    except Exception:
        logger.warning("saved tools: couldn't list them", exc_info=True)
        return json.dumps({"error": "the saved tools couldn't be read just now"})
    return json.dumps({"saved_tools": [t.summary() for t in listed]})


def usable_saved_tools(step_tools: list[str]) -> list[Any]:
    """The enabled saved tools a workflow step with these tools may run."""
    from openexecutive.config import get_settings
    from openexecutive.workflows import saved_tools

    if not get_settings().saved_tools_enabled:
        return []
    allowed = set(step_tools)
    try:
        listed = saved_tools.list_tools(enabled_only=True)
    except Exception:
        logger.warning("saved tools: couldn't list them for a step", exc_info=True)
        return []
    # Most recently changed first, capped: the list rides in the step's tool
    # definition. A tool past the cap still runs by name.
    usable = [t for t in listed if set(t.tools) <= allowed]
    usable.sort(key=lambda t: t.updated_at, reverse=True)
    return usable[:_MAX_LISTED_SAVED_TOOLS]


def _done(fields: dict[str, Any], is_error: bool) -> ScriptYield:
    return ("done", (json.dumps(fields, default=str, ensure_ascii=False), is_error))


async def run_script_tool(
    arguments: dict[str, Any],
    *,
    tools: list[str] | None,
    call: CallFn,
    origin: str,
    may_save: bool,
    may_run_saved: bool = True,
    wall_clock_s: float = _WALL_CLOCK_S,
) -> AsyncGenerator[ScriptYield, None]:
    """Answer one ``run_script`` tool use: a new script, or a saved tool.
    A script that ran yields ``("stats", {calls, duration_ms})`` just before
    its ``done``.

    ``may_save`` is whether this context may save (or re-version) a tool:
    only the principal's own interactive chat turns. A saved tool runs with
    the authority of whoever runs it, so its author must be trusted at least
    as much as anyone who may run it — never an inbound email, a teammate, an
    unattended run or a workflow step reading outside content.
    ``may_run_saved`` is whether it may run one: a workflow step (the owner
    approved its tools) or the principal's own turn, never someone else's.

    ``tools`` is a workflow step's allowlist (None in chat). A saved tool runs
    in a step only when every tool it used is one of the step's; in chat the
    gateway checks each call as usual. With ``save_as``, a script that
    succeeds is saved (approved automatically — it can only ever act through
    whatever context runs it) with the tools it called. Yields like
    ``run_script``; never raises (cancellation aside).
    """
    import time

    from openexecutive.config import get_settings
    from openexecutive.workflows import saved_tools

    saving_on = get_settings().saved_tools_enabled
    script = arguments.get("script")
    tool_name = arguments.get("tool")
    save_as = arguments.get("save_as")
    description = arguments.get("description")
    inputs = arguments.get("inputs")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        yield _done({"error": "inputs must be an object"}, True)
        return
    if len(json.dumps(inputs, default=str)) > _MAX_INPUTS_CHARS:
        yield _done({"error": "inputs are too large"}, True)
        return
    if (script is None) == (tool_name is None):
        yield _done({"error": "pass either script or tool (a saved tool's name), not both"}, True)
        return

    saved: Any = None
    if tool_name is not None:
        if not saving_on:
            yield _done({"error": "saved tools are turned off"}, True)
            return
        if save_as is not None:
            yield _done({"error": "save_as goes with a new script, not a saved tool"}, True)
            return
        if not may_run_saved:
            yield _done({"error": "saved tools run only on the principal's own turns"}, True)
            return
        try:
            saved = saved_tools.get(str(tool_name))
        except Exception:
            logger.warning("saved tool: couldn't read %r", str(tool_name)[:60], exc_info=True)
            yield _done({"error": "the saved tool couldn't be read just now; nothing ran"}, True)
            return
        if saved is None or not saved.enabled:
            yield _done({"error": f"no saved tool named {str(tool_name)[:60]!r} is turned on"}, True)
            return
        if tools is not None and not set(saved.tools) <= set(tools):
            missing = sorted(set(saved.tools) - set(tools))
            yield _done({
                "error": "this saved tool uses tools this step doesn't have",
                "missing_tools": missing,
            }, True)
            return
        script = saved.script

    # The tools the script really reached, from every call (the result's
    # calls list is capped). A step's refused names are not its tools; in
    # chat a refused call (the gateway's error) is not counted either.
    allowed = set(tools) if tools is not None else None
    reached: set[str] = set()
    call_count = 0

    async def tracked(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        nonlocal call_count
        call_count += 1
        text, failed = await call(name, args)
        if (name in allowed) if allowed is not None else not failed:
            reached.add(name)
        return text, failed

    started = time.monotonic()
    final: tuple[str, bool] | None = None
    async for kind, payload in run_script(
        str(script), tools, tracked, wall_clock_s=wall_clock_s, inputs=inputs
    ):
        if kind == "done":
            final = payload
            continue
        yield (kind, payload)
    if final is None:  # run_script always ends with done; defensive
        final = (json.dumps({"error": "the script did not finish"}), True)
    content, is_error = final
    try:
        body = json.loads(content)
    except ValueError:
        body = {"result": content}
    used = sorted(reached)

    if saved is not None:
        try:
            saved_tools.record_run(
                saved.name, saved.version, ok=not is_error, calls=call_count,
                duration_ms=int((time.monotonic() - started) * 1000), origin=origin,
            )
        except Exception:
            logger.warning("saved tool: run not recorded", exc_info=True)
        if isinstance(body, dict):
            body["saved_tool"] = {"name": saved.name, "version": saved.version}
    elif save_as is not None:
        if not saving_on:
            body["save_error"] = "saved tools are turned off"
        elif not may_save:
            body["save_error"] = "not saved: only the principal's own turns can save tools"
        elif is_error:
            body["save_error"] = "not saved: the script failed"
        else:
            try:
                kept = saved_tools.save(
                    str(save_as), str(description or ""), str(script), used, origin=origin
                )
                body["saved"] = {
                    "name": kept.name, "version": kept.version, "uses_tools": kept.tools,
                    "enabled": kept.enabled,
                }
                if not kept.enabled:
                    body["saved"]["note"] = "the owner turned this tool off; it won't run until they turn it on"
                _audit_save(kept, origin)
            except saved_tools.SavedToolError as exc:
                body["save_error"] = str(exc)
            except Exception:
                logger.warning("saved tool: couldn't save %r", str(save_as)[:60], exc_info=True)
                body["save_error"] = "not saved: storage error (the script itself ran)"
    # For the audit row and the usage summary: what the script did, without
    # adding to what the model reads.
    yield ("stats", {
        "calls": call_count,
        "duration_ms": int((time.monotonic() - started) * 1000),
    })
    yield ("done", (json.dumps(body, default=str, ensure_ascii=False), is_error))


def _audit_save(kept: Any, origin: str) -> None:
    """One audit row per save, so a new version shows beside the owner's
    own changes (saved_tool_changed)."""
    from openexecutive.audit import log_event

    try:
        log_event(
            "saved_tool_changed",
            f"Saved tool {kept.name} saved (version {kept.version})",
            actor="executive",
            details={"name": kept.name, "version": kept.version, "uses_tools": kept.tools,
                     "origin": origin},
        )
    except Exception:
        logger.warning("saved tool: couldn't audit a save", exc_info=True)


class MontyScriptShape(Exception):
    """The script awaited a future, which tools here never are."""


def available() -> bool:
    """Whether Monty is installed (it is a core dependency; this guards a
    source checkout whose environment predates it)."""
    try:
        import pydantic_monty  # noqa: F401
    except ImportError:
        return False
    return True
