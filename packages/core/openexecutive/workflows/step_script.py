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
        }
    },
    "required": ["script"],
}
_CALL_TEXT = (
    "A call returns the tool's result, parsed from JSON when it is JSON (else "
    "the text); a failed or refused call raises RuntimeError with the reason. "
    'A call that comes back {"status": "held", ...} or {"status": '
    '"waiting_for_approval", ...} is waiting for a person: do not retry it, '
    "stop the loop if the rest depends on it, and say what is waiting."
)
_SANDBOX_TEXT = (
    "The script runs in a sandbox with a subset of Python: no imports beyond "
    "json, re, math, datetime, collections, itertools and similar standard "
    "modules; no files, network or classes with inheritance. The value of the "
    "last expression is returned to you, along with anything printed and the "
    "calls the script made. If it fails partway, the calls that already ran are "
    "listed: do not repeat them."
)


def tool_definition(tools: list[str]) -> dict[str, Any]:
    """The ``run_script`` tool for a workflow step with these tools."""
    funcs = function_names(tools)
    listing = "\n".join(f"- {fn}(...)  # calls {name}" for fn, name in sorted(funcs.items()))
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
            f"{_CALL_TEXT} Every call counts against this step's tool budget and "
            "follows the same rules as calling the tool directly.\n\n"
            f"{_SANDBOX_TEXT}"
        ),
        "input_schema": _INPUT_SCHEMA,
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
        f"{_SANDBOX_TEXT}"
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
            snapshot: Any = await bounded(session.feed_start(script, print_callback=printed))
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
