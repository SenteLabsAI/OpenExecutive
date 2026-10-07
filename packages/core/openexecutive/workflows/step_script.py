"""Let a workflow action step act through one short script instead of many turns.

An action step's agent normally calls its tools one model turn at a time, so
"go through this Drive folder and file each document" costs a turn — tokens
and seconds — per file. ``run_script`` lets the agent write that loop once, in
Python, and get back only what the script returns.

The script runs in Monty (``pydantic-monty``), a sandboxed Python interpreter
for code a model wrote, in a worker process the pool spawns per script:

* **Nothing but the step's own tools.** Each tool in the step's allowlist is a
  function the script can call; there are no files, network, environment
  variables, subprocesses or imports beyond Monty's small standard library.
  Every call goes back through the action step's own per-call path
  (``action_step._StepCalls.call``): the allowlist, the call budget, the
  first-write target check (a held write comes back to the script as the
  usual ``{"status": "held", …}`` result) and the audit row — exactly as if
  the model had made the call itself. A script is a faster way to make the
  same calls, never a way around them.
* **Bounded.** Execution time (not counting time waiting on tools), memory,
  recursion and the number of tool calls are capped inside the worker, the
  whole run has a wall-clock limit, and the script's text and printed output
  are size-capped.
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
import threading
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

RUN_SCRIPT_TOOL = "run_script"

MAX_SCRIPT_CHARS = 20_000
_MAX_PRINTED_CHARS = 8_000
# Worker-enforced limits. Execution time excludes time spent waiting on the
# host (tool calls), so a script that makes many slow calls is bounded by the
# step's call budget and the wall clock below, not by this.
_LIMITS = {
    "max_feed_duration_secs": 20.0,
    "max_memory": 128 * 1024 * 1024,
    "max_recursion_depth": 200,
    # Tool calls are also counted (and refused past the step's budget) by the
    # action step; this caps the round trips a refused-call loop could make.
    "max_suspensions": 500,
}
_WALL_CLOCK_S = 600.0
_POOL_REQUEST_TIMEOUT_S = 180.0

CallFn = Callable[[str, dict[str, Any]], Coroutine[Any, Any, tuple[str, bool]]]

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


def tool_definition(tools: list[str]) -> dict[str, Any]:
    """The ``run_script`` tool for a step with these tools."""
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
            "call_tool(name, arguments) also reaches any of them by exact name.\n"
            "A call returns the tool's result, parsed from JSON when it is JSON "
            "(else the text); a failed call raises RuntimeError with the tool's "
            "error. A write held for the owner's approval returns "
            '{"status": "held", ...}: do not retry it.\n\n'
            "The script runs in a sandbox with a subset of Python: no imports "
            "beyond json, re, math, datetime, collections, itertools and similar "
            "standard modules; no files, network or classes with inheritance. "
            "The value of the last expression is returned to you, along with "
            "anything printed. Every call counts against this step's tool budget "
            "and follows the same rules as calling the tool directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Python source. End with an expression whose value you want back.",
                }
            },
            "required": ["script"],
        },
    }


def _parse_result(content: str) -> Any:
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return content


class _ScriptError(Exception):
    """A fixed-message failure to report to the model."""


def _run_sync(
    script: str,
    tools: list[str],
    call: CallFn,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> tuple[Any, str]:
    """Run ``script`` in a fresh Monty worker. Called in a worker thread; each
    tool call is handed back to ``loop`` (where the gateway lives) and waited
    on here, so the script sees ordinary function calls."""
    from pydantic_monty import CollectString, Monty

    allowed = set(tools)

    def invoke(name: str, arguments: dict[str, Any]) -> Any:
        # Set when the run gave up on this script (wall clock): no call may
        # start after the step has moved on.
        if stop.is_set():
            raise RuntimeError("the script was stopped")
        if name not in allowed:
            raise ValueError(f"{name} is not one of this step's tools")
        future = asyncio.run_coroutine_threadsafe(call(name, arguments), loop)
        content, is_error = future.result()
        if is_error:
            raise RuntimeError(f"{name} failed: {content}")
        return _parse_result(content)

    def bind(name: str) -> Callable[..., Any]:
        def fn(*args: Any, **kwargs: Any) -> Any:
            if args:
                if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
                    return invoke(name, args[0])
                raise TypeError(f"{name}: pass the tool's arguments by name")
            return invoke(name, kwargs)

        return fn

    def call_tool(name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return invoke(str(name), {**(arguments or {}), **kwargs})

    external: dict[str, Any] = {fn: bind(name) for fn, name in function_names(tools).items()}
    external["call_tool"] = call_tool
    printed = CollectString(max_bytes=_MAX_PRINTED_CHARS * 4)
    with (
        Monty(min_processes=1, max_processes=1, request_timeout=_POOL_REQUEST_TIMEOUT_S) as pool,
        pool.checkout(
            script_name="step_script.py",
            limits=_LIMITS,  # type: ignore[arg-type]
            os_policy={"sleep": "zero"},
        ) as session,
    ):
        result = session.feed_run(script, external_lookup=external, print_callback=printed)
    return result, printed.output


async def run_script(script: str, tools: list[str], call: CallFn) -> tuple[str, bool]:
    """Run a step script. Returns (result text for the model, is_error); never raises."""
    from pydantic_monty import MontyError

    if not script.strip():
        return json.dumps({"error": "the script is empty"}), True
    if len(script) > MAX_SCRIPT_CHARS:
        return json.dumps({"error": f"the script is longer than {MAX_SCRIPT_CHARS} characters"}), True
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    try:
        result, printed = await asyncio.wait_for(
            asyncio.to_thread(_run_sync, script, tools, call, loop, stop), timeout=_WALL_CLOCK_S
        )
    except TimeoutError:
        stop.set()
        return json.dumps({"error": "the script ran past its time limit"}), True
    except MontyError as exc:
        # What the script did wrong (its own traceback), so the model can fix
        # it. It names only the script's code and values it handled — data the
        # model already saw.
        try:
            display = getattr(exc, "display", None)
            detail = str(display()) if callable(display) else f"{type(exc).__name__}: {exc}"
        except Exception:  # noqa: BLE001
            detail = type(exc).__name__
        return json.dumps({"error": "the script failed", "detail": detail[-4_000:]}), True
    except Exception as exc:
        logger.warning("step script: runner failed (%s)", type(exc).__name__)
        return json.dumps({"error": "the script could not be run"}), True
    payload: dict[str, Any] = {"result": result}
    if printed:
        payload["printed"] = printed[-_MAX_PRINTED_CHARS:]
    return json.dumps(payload, default=str, ensure_ascii=False), False


def available() -> bool:
    """Whether Monty is installed (it is a core dependency; this guards a
    source checkout whose environment predates it)."""
    try:
        import pydantic_monty  # noqa: F401
    except ImportError:
        return False
    return True
