"""Run a document parser in a short-lived child process.

Parsing a large PDF (pypdf's object graph, pdfium's page bitmaps, the OCR
model) allocates hundreds of MB of small objects. CPython's allocator keeps
the arenas they lived in, so a parser run inside the API process left its
peak behind for good: a 38 MB, 131-page PDF took uvicorn from ~270 MB to
~380 MB for the rest of its life, and OCR of a scan took it past 780 MB at
the peak. ``run_isolated`` runs the parser in a fresh interpreter instead,
and everything it allocated goes back to the OS when that process exits.

The call runs ``func(*args)`` in ``python -m openexecutive.knowledge.isolated``.
``func`` must be a module-level function (it is sent by reference) and its
result must be JSON-serialisable: text, numbers, lists. The arguments travel
to the child pickled on stdin; the answer comes back as JSON on stdout, so
nothing the child prints is ever unpickled here. Both of the child's output
streams go to temp files, not this process's memory: the child logs nothing
(a hostile file can make a parser log without end), only the last
``_MAX_STDERR_LOGGED`` characters of its stderr are ever read back, and an
answer past ``_MAX_ANSWER_BYTES`` is refused unread. The child's environment
holds only what it needs to start Python, so the deployment's keys are not
in it. That is hygiene, not a sandbox: the child runs as the same user and
can read what this process can.

An exception raised by ``func`` is raised again here when its class is one
the caller names in ``reraise``, and as ``IsolatedError`` otherwise. A child
that dies without answering (OOM kill, crash) or runs past ``timeout``
raises ``WorkerStopped``, and so does an answer too large to take back.
When no child can be started at all, ``func`` runs in this process instead,
as it did before isolation.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import pickle
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Tests that monkeypatch a parser's internals set this to False so the
# patched code runs in-process, where the patch applies.
enabled = True

# What the child inherits from this process's environment: enough to find
# and start this Python and its packages, and nothing else.
_ENV_KEEP = frozenset({
    "PATH", "HOME", "LANG", "LANGUAGE", "TMPDIR", "TEMP", "TMP",
    "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "VIRTUAL_ENV", "SYSTEMROOT",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
})
# Run with -m, this module is __main__ in the child, so it names itself.
_MODULE = "openexecutive.knowledge.isolated"
_MAX_STDERR_LOGGED = 2000
# The largest answer taken back from the child, as UTF-8 JSON. A 2,000-page
# text layer is a few MB; past this, the parse is refused rather than loaded.
_MAX_ANSWER_BYTES = 32 * 1024 * 1024
_TOO_LARGE = "AnswerTooLarge"

_warned_spawn_failure = False


class IsolatedError(RuntimeError):
    """The isolated call raised an exception the caller did not name."""


class WorkerStopped(IsolatedError):
    """The child process died or was killed before it answered."""


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith("LC_")}
    # Keep the child's numeric libraries (ONNX Runtime, OpenCV) to one thread
    # pool each, as small as the machine allows.
    env.setdefault("OMP_NUM_THREADS", "1")
    return env


def _rebuild(error: dict[str, Any], reraise: tuple[type[BaseException], ...]) -> BaseException:
    name = f"{error.get('module')}:{error.get('type')}"
    for cls in reraise:
        if f"{cls.__module__}:{cls.__qualname__}" == name:
            exc = cls.__new__(cls)
            exc.args = tuple(error.get("args") or ())
            exc.__dict__.update(error.get("attrs") or {})
            return exc
    return IsolatedError(f"{error.get('type')}: {error.get('message')}")


def run_isolated(
    func: Callable[..., T],
    *args: Any,
    timeout: float,
    reraise: tuple[type[BaseException], ...] = (),
) -> T:
    """Return ``func(*args)``, computed in a child process. Blocking — run it
    in a thread from async code."""
    global _warned_spawn_failure

    if not enabled:
        return func(*args)
    request = pickle.dumps((func.__module__, func.__qualname__, args), pickle.HIGHEST_PROTOCOL)
    name = func.__qualname__
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", _MODULE],
                input=request,
                stdout=out,
                stderr=err,
                timeout=timeout,
                env=_child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerStopped(f"{name} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            if not _warned_spawn_failure:
                _warned_spawn_failure = True
                logger.warning(
                    "isolated: could not start a parser process (%s); parsing in-process",
                    type(exc).__name__,
                )
            return func(*args)
        del request

        size = out.seek(0, os.SEEK_END)
        if size > _MAX_ANSWER_BYTES:
            raise WorkerStopped(f"{name} answered with {size} bytes, past the limit")
        out.seek(0)
        try:
            reply = json.loads(out.read())
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or "ok" not in reply:
            err.seek(max(0, err.seek(0, os.SEEK_END) - _MAX_STDERR_LOGGED))
            logger.warning(
                "isolated: %s exited with code %s and no answer: %s",
                name, proc.returncode, err.read().decode("utf-8", "replace"),
            )
            raise WorkerStopped(f"{name} stopped with exit code {proc.returncode}")
    if reply["ok"]:
        return reply["result"]  # type: ignore[no-any-return]
    error = reply.get("error") or {}
    if error.get("type") == _TOO_LARGE and error.get("module") == _MODULE:
        raise WorkerStopped(f"{name} answer was past the limit")
    raise _rebuild(error, reraise)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value


def _main() -> int:
    # The answer gets fd 1 to itself: anything a library prints, from Python
    # or C, goes to stderr instead. Nothing is logged: pypdf logs text taken
    # from the file, and a crafted one can make it log without end.
    answer = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")

    module, qualname, args = pickle.loads(sys.stdin.buffer.read())
    func: Any = importlib.import_module(module)
    for part in qualname.split("."):
        func = getattr(func, part)
    try:
        reply: dict[str, Any] = {"ok": True, "result": func(*args)}
    except Exception as exc:
        attrs = {k: v for k, v in vars(exc).items() if _jsonable(v) is not None}
        reply = {
            "ok": False,
            "error": {
                "module": type(exc).__module__,
                "type": type(exc).__qualname__,
                "message": str(exc)[:1000],
                "args": [a for a in exc.args if _jsonable(a) is not None],
                "attrs": attrs,
            },
        }
    out = json.dumps(reply, ensure_ascii=False).encode()
    if len(out) > _MAX_ANSWER_BYTES:
        out = json.dumps({
            "ok": False,
            "error": {"module": _MODULE, "type": _TOO_LARGE, "message": f"{len(out)} bytes"},
        }).encode()
    answer.write(out)
    answer.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
