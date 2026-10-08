"""Python jobs: library-heavy work on files (split a PDF, build a spreadsheet,
draw a chart) in a WebAssembly sandbox.

``run_script`` (Monty) decides what to do and calls the Executive's tools;
it can't run libraries. ``run_python_job`` is the workshop for that: Python
in Pyodide (WebAssembly) with a fixed, bundled set of libraries (pypdf,
openpyxl, pandas, numpy, matplotlib, Pillow, lxml, python-docx, python-pptx,
fpdf2, XlsxWriter), run by Deno in a process of its own per job.

**The sandbox is Deno, not Pyodide.** Pyodide's Python can call into its
JavaScript runtime, so under plain Node a job could read the server's
environment (API keys), its files and the network. Deno is started with read
access to the sandbox folder and the worker script only: no network, no
environment, no writes, no processes. Files go in on stdin and come back on
stdout, so the job never needs a file or network permission.

A job sees only what it is handed: files from the Knowledge library, named
by the caller; the files attached to the chat message being answered, as
sent (workflows/turn_files.py); text the caller passes; and ``inputs``.
Result files are kept for ``_KEEP_DAYS`` under the company folder and served
to the principal at ``GET /python-jobs/{job}/{file}``.

A job that worked can be kept as a Custom tool (``save_as``, a saved tool of
kind ``python``, workflows/saved_tools.py) and run again by name with new
``inputs`` and files, so a job done before costs no code-writing tokens.
Every job is metered: one ``python_job`` audit row with its time, CPU,
memory and bytes, which ``GET /audit/usage`` sums (``python_jobs``).

The sandbox is installed by the API image (``docker/Dockerfile``) under
``PYTHON_SANDBOX_DIR``; ``available()`` is false without it, and the tool
is then not offered.

A deployment can run jobs elsewhere instead: with ``PYTHON_JOB_RUNNER_URL``
set, ``run_job`` POSTs each job there (``_run_remote``) and reads back the
same result object the local worker prints, so files, caps, kept results and
metering work the same. The runner is trusted to isolate jobs as the local
sandbox does.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import resource
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOOL_NAME = "run_python_job"
_WORKER = Path(__file__).with_name("python_job_worker.mjs")
MAX_CODE_CHARS = 20_000
_MAX_INPUT_FILES = 10
_MAX_INPUT_BYTES = 25 * 2**20
_MAX_OUTPUT_FILES = 20
_MAX_OUTPUT_BYTES = 25 * 2**20
# What the API reads back from a job: the output files base64'd (4/3) plus
# result and printed text. Anything bigger is refused unread, so a job can't
# make the API itself run out of memory.
_MAX_STDOUT_BYTES = _MAX_OUTPUT_BYTES * 4 // 3 + 2**20
# V8's own heap, and the WebAssembly memory Python lives in (pages of 64 KiB:
# 768 MB). Buffers outside both are held by the process data limit
# (PYTHON_JOB_MEMORY_MB, set in _limit_memory).
_V8_FLAGS = "--max-old-space-size=256,--wasm-max-mem-pages=12288"
_KEEP_DAYS = 7
# All kept result files together; the oldest jobs go first past it. The
# company volume also holds the database and the knowledge base.
_KEEP_TOTAL_BYTES = 200 * 2**20
# Jobs prune after they finish, outside the job slot.
_prune_lock = threading.Lock()
# One job at a time per process: each takes 200-450 MB while it runs.
_slot = asyncio.Semaphore(1)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,120}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")

_MAX_INPUTS_CHARS = 20_000

TOOL_DEFINITION: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Run Python with real libraries on files, when a job needs more than your "
        "tools: split or merge PDFs and read their pages (pypdf), read or build "
        "spreadsheets (openpyxl, pandas), analyse data (pandas, numpy), draw charts "
        "(matplotlib), work with images (Pillow), and make documents, from scratch "
        "or from files: Word (python-docx, imported as docx), PowerPoint "
        "(python-pptx, as pptx), laid-out PDFs (fpdf2, as fpdf), Excel with charts "
        "(openpyxl, xlsxwriter). These make files to download; for a Google Doc, "
        "Sheet or Slides use the connected Google Workspace tools instead. Input "
        "files are under /in, named "
        "as given; write result files to /out and the person gets a download link "
        "for each. The value of the last expression and anything printed come back "
        "to you. The sandbox has no network and no other files; only the libraries "
        "listed are installed. Pass files attached to the person's message by name "
        "in attachment_files (the file itself, never retype its text), "
        "Knowledge-library files by name in knowledge_files, and other text you "
        "already have (data from a tool) in text_files. Each run starts fresh and "
        "takes a few seconds, so do the whole job in one call. To keep a job that "
        "worked for next time, also pass save_as (a snake_case name) and "
        "description (one sentence on what it does and which files and inputs it "
        "takes); write such code to read its per-run values from the dict "
        "`inputs`. list_saved_tools shows kept jobs (run_with run_python_job); run "
        "one with tool (its name), inputs and its files instead of code."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source. Instead of tool."},
            "tool": {
                "type": "string",
                "description": "Instead of code: the name of a kept Python job to run again.",
            },
            "inputs": {
                "type": "object",
                "description": "Values for this run, read by the code as the dict `inputs`.",
            },
            "attachment_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of files attached to the person's message, copied to /in as sent.",
            },
            "knowledge_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File names from the Knowledge library, copied to /in.",
            },
            "text_files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Name -> text content, written to /in as text files.",
            },
            "save_as": {
                "type": "string",
                "description": "Optional, with code: keep the job as a Custom tool under this snake_case name if it works.",
            },
            "description": {
                "type": "string",
                "description": "With save_as: one sentence on what it does and which files and inputs it takes.",
            },
        },
    },
}


def _sandbox_dir() -> Path:
    from openexecutive.config import get_settings

    return Path(get_settings().python_sandbox_dir)


def available() -> bool:
    """Whether jobs are on and something can run them: a remote runner
    (``PYTHON_JOB_RUNNER_URL``) or the local sandbox."""
    from openexecutive.config import get_settings

    settings = get_settings()
    if not settings.python_jobs_enabled:
        return False
    if settings.python_job_runner_url:
        return True
    base = _sandbox_dir()
    return (base / "deno").is_file() and (base / "pyodide" / "pyodide.mjs").is_file()


def jobs_dir() -> Path:
    from openexecutive.config import get_settings

    return get_settings().company_profile_path.parent / "python_jobs"


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _limit_memory(limit_mb: int) -> Any:
    """For the sandbox process, before it runs: a data limit (RLIMIT_DATA),
    which also bounds memory the V8 flags don't (JavaScript buffers); first in
    line if the machine runs out of memory, so the job dies and not the API;
    and a lower CPU priority, so chat and the scheduler stay responsive."""

    def apply() -> None:
        size = limit_mb * 2**20
        resource.setrlimit(resource.RLIMIT_DATA, (size, size))
        try:
            with open("/proc/self/oom_score_adj", "w") as f:
                f.write("1000")
        except OSError:
            pass
        os.nice(10)

    return apply


async def _read_line(stream: asyncio.StreamReader, limit: int) -> bytes | None:
    """Read up to the first newline (or the end), or None once past ``limit``.
    The worker prints its result as one line of JSON."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(2**16):
        end = chunk.find(b"\n")
        if end >= 0:
            chunk = chunk[:end]
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if end >= 0:
            break
    return b"".join(chunks)


async def _read_head(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read a stream to its end, keeping its first ``limit`` bytes: draining
    the rest keeps the process from blocking on a full pipe."""
    kept = b""
    while chunk := await stream.read(2**16):
        if len(kept) < limit:
            kept += chunk[: limit - len(kept)]
    return kept


async def run_job(
    code: str,
    files: dict[str, bytes],
    *,
    timeout_s: float,
    memory_mb: int = 1536,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one job in a fresh sandbox process, which exits when the job ends.
    Never raises (cancellation aside, which kills the process). The reply
    carries ``_usage`` ({peak_mb, cpu_ms}, either None when unknown), read
    from the process itself rather than from anything the job printed.
    With ``PYTHON_JOB_RUNNER_URL`` set the job runs there instead."""
    from openexecutive.config import get_settings

    settings = get_settings()
    if settings.python_job_runner_url:
        return await _run_remote(
            settings.python_job_runner_url, settings.python_job_runner_key or "",
            code, files, timeout_s=timeout_s, memory_mb=memory_mb, inputs=inputs,
        )
    base = _sandbox_dir()
    wheels = sorted(str(p) for p in (base / "wheels").glob("*.whl"))
    job = json.dumps({
        "code": code,
        "inputs": inputs or {},
        "pyodide": str(base / "pyodide"),
        "wheels": wheels,
        "files": {n: base64.b64encode(b).decode() for n, b in files.items()},
        "max_file_bytes": _MAX_OUTPUT_BYTES,
        "max_total_bytes": _MAX_OUTPUT_BYTES,
        "max_files": _MAX_OUTPUT_FILES,
    }).encode() + b"\n"
    async with _slot:
        # Deno writes its own caches there whatever the job's permissions,
        # and a job can feed them: a folder of the job's own, off the company
        # volume, gone when the job ends (--no-code-cache keeps it small).
        deno_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="pyjob-")
        try:
            return await _run_in(base, job, deno_dir, timeout_s=timeout_s, memory_mb=memory_mb)
        finally:
            await asyncio.to_thread(shutil.rmtree, deno_dir, True)


def _runner_client(timeout_s: float) -> httpx.AsyncClient:
    # Proxies from the environment are fine; redirects are not followed, so
    # the job and the key go to the configured URL only.
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0), follow_redirects=False)


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**40 else None


async def _run_remote(
    url: str, key: str, code: str, files: dict[str, bytes], *,
    timeout_s: float, memory_mb: int, inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run one job on the remote runner. The request is the job the local
    worker reads (less its sandbox paths) plus the time and memory limits;
    the reply is the worker's result object, with ``usage`` ({peak_mb,
    cpu_ms}) measured by the runner. Never raises (cancellation aside); the
    reply is read only up to the cap a local job's stdout has, and only its
    known fields are kept."""
    job = {
        "code": code,
        "inputs": inputs or {},
        "files": {n: base64.b64encode(b).decode() for n, b in files.items()},
        "max_file_bytes": _MAX_OUTPUT_BYTES,
        "max_total_bytes": _MAX_OUTPUT_BYTES,
        "max_files": _MAX_OUTPUT_FILES,
        "timeout_s": timeout_s,
        "memory_mb": memory_mb,
    }
    usage: dict[str, int | None] = {"peak_mb": None, "cpu_ms": None}
    try:
        # The runner applies timeout_s to the job itself; the margin covers
        # sending the files and getting the results back.
        async with _runner_client(timeout_s + 60) as client, client.stream(
            "POST", url, json=job, headers={"Authorization": f"Bearer {key}"},
        ) as resp:
            if resp.status_code != 200:
                logger.warning("python job: runner answered HTTP %s", resp.status_code)
                return {"error": f"the job runner could not run the job (HTTP {resp.status_code})", "_usage": usage}
            raw = bytearray()
            async for chunk in resp.aiter_bytes():
                raw += chunk
                if len(raw) > _MAX_STDOUT_BYTES:
                    return {"error": "the job's output was too large; nothing was kept", "_usage": usage}
    except httpx.TimeoutException:
        return {"error": f"the job ran past its {int(timeout_s)} s limit", "_usage": usage}
    except httpx.HTTPError as exc:
        logger.warning("python job: runner unreachable: %s", type(exc).__name__)
        return {"error": "the job runner could not be reached", "_usage": usage}
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return {"error": "the job runner sent back something unreadable", "_usage": usage}
    reported = body.get("usage")
    if not isinstance(reported, dict):
        reported = {}
    usage = {"peak_mb": _count(reported.get("peak_mb")), "cpu_ms": _count(reported.get("cpu_ms"))}
    out: dict[str, Any] = {"_usage": usage}
    for field, limit in (("result", _MAX_INPUTS_CHARS), ("error", 2000), ("printed", 8000)):
        if (text := _text(body.get(field), limit)) is not None:
            out[field] = text
    if isinstance(body.get("files"), dict):
        out["files"] = body["files"]
    if isinstance(body.get("skipped"), list):
        out["skipped"] = [n for n in body["skipped"] if isinstance(n, str)][:_MAX_OUTPUT_FILES]
    return out


def _proc_usage(pid: int) -> dict[str, int | None]:
    """The process's peak memory (VmHWM) and CPU time so far, from /proc;
    None for what can't be read (it already exited, or no /proc)."""
    peak_mb: int | None = None
    cpu_ms: int | None = None
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                peak_mb = int(line.split()[1]) // 1024
                break
        # Fields after the parenthesised name; utime and stime are 14 and 15.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_ms = (int(fields[11]) + int(fields[12])) * 1000 // ticks
    except (OSError, ValueError, IndexError):
        pass
    return {"peak_mb": peak_mb, "cpu_ms": cpu_ms}


async def _run_in(
    base: Path, job: bytes, deno_dir: str, *, timeout_s: float, memory_mb: int
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        str(base / "deno"), "run", "--no-prompt", "--quiet", "--no-remote", "--no-code-cache",
        f"--v8-flags={_V8_FLAGS}", f"--allow-read={base},{_WORKER}", str(_WORKER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Nothing of the server's environment: no keys, no proxies.
        env={"PATH": "/usr/bin:/bin", "HOME": str(base), "DENO_NO_UPDATE_CHECK": "1",
             "DENO_DIR": deno_dir},
        preexec_fn=_limit_memory(memory_mb),
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    usage: dict[str, int | None] = {"peak_mb": None, "cpu_ms": None}

    def stop() -> None:
        # Measured just before the kill, while /proc still has it.
        usage.update(_proc_usage(proc.pid))
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        if proc.stdin is not None:
            proc.stdin.close()

    async def exchange() -> tuple[bytes | None, bytes]:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        # One line of JSON; stdin stays open so the worker waits, after its
        # result, for stop() to measure it.
        proc.stdin.write(job)
        await proc.stdin.drain()
        err_task = asyncio.create_task(_read_head(proc.stderr, 2**16))
        out = await _read_line(proc.stdout, _MAX_STDOUT_BYTES)
        # The result line is in hand, stdout ended, or it passed its cap:
        # stop the process now. Waiting for it to exit would let job code that
        # keeps running (a Worker, a disabled Deno.exit, a closed stdout) hold
        # the slot to the timeout.
        stop()
        err = await err_task
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(exchange(), timeout=timeout_s)
    except TimeoutError:
        stop()
        await proc.wait()
        return {"error": f"the job ran past its {int(timeout_s)} s limit", "_usage": usage}
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if out is None:
        return {"error": "the job's output was too large; nothing was kept", "_usage": usage}
    # The exit code doesn't count once the worker printed its result: the
    # process is killed as soon as stdout ends.
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    if isinstance(body, dict):
        body["_usage"] = usage
        return body
    detail = (err or b"").decode(errors="replace")[-400:]
    logger.warning("python job: sandbox exited %s: %s", proc.returncode, detail)
    return {"error": "the job stopped: it may have run out of memory or crashed", "_usage": usage}


def _knowledge_file(name: str) -> Path | None:
    from openexecutive.orchestrator.document_tools import _company_docs_dir

    if not _SAFE_NAME.match(name):
        return None
    docs = _company_docs_dir()
    path = (docs / name).resolve()
    return path if path.is_file() and path.is_relative_to(docs) else None


def _prune(root: Path) -> None:
    """Drop jobs past their days, then the oldest while all kept files pass
    _KEEP_TOTAL_BYTES."""
    with _prune_lock:
        try:
            _prune_unlocked(root)
        except FileNotFoundError:
            # A folder went while it was being measured; the next job's
            # prune picks up the rest.
            logger.info("python job: a result folder went while pruning", exc_info=True)


def _prune_unlocked(root: Path) -> None:
    if not root.is_dir():
        return
    # Deno's cache from before jobs had a folder of their own.
    shutil.rmtree(root / ".deno", ignore_errors=True)
    cutoff = time.time() - _KEEP_DAYS * 86400
    jobs: list[tuple[float, int, Path]] = []
    for child in root.iterdir():
        if not _JOB_ID.match(child.name):
            continue
        mtime = child.stat().st_mtime
        if mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            continue
        size = sum(f.stat().st_size for f in child.iterdir() if f.is_file())
        jobs.append((mtime, size, child))
    total = sum(size for _, size, _ in jobs)
    for _, size, child in sorted(jobs, key=lambda j: j[0]):
        if total <= _KEEP_TOTAL_BYTES:
            break
        shutil.rmtree(child, ignore_errors=True)
        total -= size


async def _gather_files(tool_input: dict[str, Any]) -> tuple[dict[str, bytes], list[str]] | str:
    """The job's input files and the attachments among them, or an error."""
    from openexecutive.workflows import turn_files

    files: dict[str, bytes] = {}
    attached: list[str] = []
    for name in tool_input.get("attachment_files") or []:
        data = turn_files.get(str(name))
        if data is None:
            names = turn_files.names()
            return _err(
                f"no file named {str(name)[:80]!r} is attached to this message"
                + (f"; attached: {', '.join(names)}" if names else "; nothing is attached")
            )
        name = turn_files.safe_name(str(name))
        files[name] = data
        attached.append(name)
    for name in tool_input.get("knowledge_files") or []:
        path = _knowledge_file(str(name))
        if path is None:
            return _err(f"no Knowledge file named {str(name)[:80]!r}")
        files[path.name] = await asyncio.to_thread(path.read_bytes)
    text_files = tool_input.get("text_files") or {}
    if not isinstance(text_files, dict):
        return _err("text_files must map names to text")
    for name, text in text_files.items():
        if not _SAFE_NAME.match(str(name)) or not isinstance(text, str):
            return _err(f"bad text file {str(name)[:80]!r}")
        files[str(name)] = text.encode()
    if len(files) > _MAX_INPUT_FILES or sum(map(len, files.values())) > _MAX_INPUT_BYTES:
        return _err("too many or too large input files")
    return files, attached


async def handle_run_python_job(tool_input: dict[str, Any]) -> str:
    from openexecutive.config import get_settings
    from openexecutive.workflows import saved_tools

    if not available():
        return _err("Python jobs aren't available on this server.")
    settings = get_settings()
    code = tool_input.get("code")
    tool_name = tool_input.get("tool")
    save_as = tool_input.get("save_as")
    inputs = tool_input.get("inputs")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict) or len(json.dumps(inputs, default=str)) > _MAX_INPUTS_CHARS:
        return _err("inputs must be an object of at most 20,000 characters as JSON")
    if (code is None) == (tool_name is None):
        return _err("pass either code or tool (a kept job's name), not both")

    saved: Any = None
    if tool_name is not None:
        if not settings.saved_tools_enabled:
            return _err("Custom tools are turned off")
        if save_as is not None:
            return _err("save_as goes with new code, not a kept job")
        try:
            saved = saved_tools.get(str(tool_name))
        except Exception:
            logger.warning("python job: couldn't read saved tool %r", str(tool_name)[:60], exc_info=True)
            return _err("the kept job couldn't be read just now; nothing ran")
        if saved is None or not saved.enabled:
            # list_saved_tools comes with run_script, which an install may not
            # offer, so the kept jobs are named here too.
            kept = [t.name for t in saved_tools.list_tools(enabled_only=True) if t.kind == "python"]
            return _err(
                f"no Custom tool named {str(tool_name)[:60]!r} is turned on"
                + (f"; kept Python jobs: {', '.join(kept[:30])}" if kept else "")
            )
        if saved.kind != "python":
            return _err(f"{saved.name!r} is a script tool: run it with run_script(tool=...)")
        code = saved.script
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
        return _err(f"code must be Python source of at most {MAX_CODE_CHARS} characters")

    gathered = await _gather_files(tool_input)
    if isinstance(gathered, str):
        return gathered
    files, attached = gathered

    started = time.monotonic()
    body = await run_job(
        code, files, timeout_s=settings.python_job_timeout_s,
        memory_mb=settings.python_job_memory_mb, inputs=inputs,
    )
    usage = body.pop("_usage", None) or {}
    reported_rss = body.pop("rss_mb", None)
    out_files = body.pop("files", None) or {}
    reply: dict[str, Any] = {
        k: v for k, v in body.items() if k in ("result", "error", "printed", "skipped") and v
    }
    kept_bytes = 0
    if out_files:
        encoded = {
            n: b for n, b in out_files.items()
            if isinstance(n, str) and isinstance(b, str) and _SAFE_NAME.match(n)
        }
        # Sizes checked on the encoded text, before anything is decoded.
        too_big = sum(len(b) for b in encoded.values()) * 3 // 4 > _MAX_OUTPUT_BYTES
        if len(encoded) > _MAX_OUTPUT_FILES or too_big:
            reply["files_error"] = "the result files are too many or too large; none kept"
        else:
            decoded = {n: base64.b64decode(b) for n, b in encoded.items()}
            kept_bytes = sum(map(len, decoded.values()))
            reply.update(await _keep(decoded))
    # After keeping, so the new files count toward the total.
    await asyncio.to_thread(_prune, jobs_dir())
    ok = "error" not in reply and "files_error" not in reply
    duration_ms = int((time.monotonic() - started) * 1000)
    reply["seconds"] = round(duration_ms / 1000, 1)

    if saved is not None:
        try:
            saved_tools.record_run(
                saved.name, saved.version, ok=ok, calls=0, duration_ms=duration_ms, origin="chat"
            )
        except Exception:
            logger.warning("python job: run of %r not recorded", saved.name, exc_info=True)
        reply["saved_tool"] = {"name": saved.name, "version": saved.version}
    elif save_as is not None:
        reply.update(_save(str(save_as), str(tool_input.get("description") or ""), code, ok))

    peak_mb = usage.get("peak_mb")
    if peak_mb is None and isinstance(reported_rss, int) and 0 <= reported_rss < 2**20:
        peak_mb = reported_rss
    _meter({
        "ok": ok,
        "duration_ms": duration_ms,
        "cpu_ms": usage.get("cpu_ms"),
        "peak_mb": peak_mb,
        "files_in": len(files),
        "bytes_in": sum(map(len, files.values())),
        "attachments": len(attached),
        "files_out": len(reply.get("files") or []),
        "bytes_out": kept_bytes,
        "code_chars": len(code),
        "saved_tool": saved.name if saved is not None else None,
        "saved_as": (reply.get("saved") or {}).get("name"),
    })
    return json.dumps(reply, ensure_ascii=False)


def _save(name: str, description: str, code: str, ok: bool) -> dict[str, Any]:
    """Keep a job that worked as a Custom tool of kind python. The tool is
    principal-only (PRINCIPAL_ONLY_TOOLS), so this is the principal's own turn,
    and a Python tool never runs in workflows."""
    from openexecutive.config import get_settings
    from openexecutive.workflows import saved_tools
    from openexecutive.workflows.step_script import _audit_save

    if not get_settings().saved_tools_enabled:
        return {"save_error": "Custom tools are turned off"}
    if not ok:
        return {"save_error": "not saved: the job failed"}
    try:
        kept = saved_tools.save(name, description, code, [], origin="chat", kind="python")
    except saved_tools.SavedToolError as exc:
        return {"save_error": str(exc)}
    except Exception:
        logger.warning("python job: couldn't save %r", name[:60], exc_info=True)
        return {"save_error": "not saved: storage error (the job itself ran)"}
    _audit_save(kept, "chat")
    saved: dict[str, Any] = {"name": kept.name, "version": kept.version, "enabled": kept.enabled}
    if not kept.enabled:
        saved["note"] = "the owner turned this tool off; it won't run until they turn it on"
    return {"saved": saved}


def _meter(details: dict[str, Any]) -> None:
    """One audit row per job, summed by GET /audit/usage (python_jobs). The
    turn's model tokens are already in its cache_event rows."""
    from openexecutive.audit import log_event

    try:
        log_event(
            "python_job",
            f"Python job {'worked' if details['ok'] else 'failed'} in {details['duration_ms'] / 1000:.1f}s",
            actor="executive",
            details=details,
        )
    except Exception:
        logger.warning("python job: couldn't record its usage", exc_info=True)


async def _keep(files: dict[str, bytes]) -> dict[str, Any]:
    """Store a job's result files and return their links for the reply."""
    job_id = uuid.uuid4().hex
    folder = jobs_dir() / job_id
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    for name, data in files.items():
        await asyncio.to_thread((folder / name).write_bytes, data)
    return {
        "files": [
            {"name": n, "bytes": len(d),
             "link": f"/api/backend/python-jobs/{job_id}/{urllib.parse.quote(n)}"}
            for n, d in sorted(files.items())
        ],
        "note": "Give the person each file's link as a markdown link.",
    }


def result_file(job_id: str, name: str) -> Path | None:
    """A kept result file, or None (bad id or name, or gone)."""
    if not _JOB_ID.match(job_id) or not _SAFE_NAME.match(name):
        return None
    root = jobs_dir().resolve()
    path = (root / job_id / name).resolve()
    return path if path.is_file() and path.is_relative_to(root) else None


PYTHON_JOB_TOOLS: list[dict[str, Any]] = [TOOL_DEFINITION]
PYTHON_JOB_TOOL_HANDLERS = {TOOL_NAME: handle_run_python_job}
