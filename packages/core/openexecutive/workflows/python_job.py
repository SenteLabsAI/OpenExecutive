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
by the caller, and text the caller passes (for example, an attachment's
extracted text). Result files are kept for ``_KEEP_DAYS`` under the company
folder and served to the principal at ``GET /python-jobs/{job}/{file}``.

The sandbox is installed by the API image (``docker/Dockerfile``) under
``PYTHON_SANDBOX_DIR``; ``available()`` is false without it, and the tool
is then not offered.
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
        "listed are installed. Pass Knowledge-library files by name in "
        "knowledge_files, and text you already have (such as an attachment's "
        "extracted text or data from a tool) in text_files. Each run starts fresh "
        "and takes a few seconds, so do the whole job in one call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source."},
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
        },
        "required": ["code"],
    },
}


def _sandbox_dir() -> Path:
    from openexecutive.config import get_settings

    return Path(get_settings().python_sandbox_dir)


def available() -> bool:
    """Whether the sandbox is installed and jobs are on."""
    from openexecutive.config import get_settings

    if not get_settings().python_jobs_enabled:
        return False
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
    code: str, files: dict[str, bytes], *, timeout_s: float, memory_mb: int = 1536
) -> dict[str, Any]:
    """Run one job in a fresh sandbox process, which exits when the job ends.
    Never raises (cancellation aside, which kills the process)."""
    base = _sandbox_dir()
    wheels = sorted(str(p) for p in (base / "wheels").glob("*.whl"))
    job = json.dumps({
        "code": code,
        "pyodide": str(base / "pyodide"),
        "wheels": wheels,
        "files": {n: base64.b64encode(b).decode() for n, b in files.items()},
        "max_file_bytes": _MAX_OUTPUT_BYTES,
        "max_total_bytes": _MAX_OUTPUT_BYTES,
        "max_files": _MAX_OUTPUT_FILES,
    }).encode()
    async with _slot:
        # Deno writes its own caches there whatever the job's permissions,
        # and a job can feed them: a folder of the job's own, off the company
        # volume, gone when the job ends (--no-code-cache keeps it small).
        deno_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="pyjob-")
        try:
            return await _run_in(base, job, deno_dir, timeout_s=timeout_s, memory_mb=memory_mb)
        finally:
            await asyncio.to_thread(shutil.rmtree, deno_dir, True)


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

    async def exchange() -> tuple[bytes | None, bytes]:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        proc.stdin.write(job)
        await proc.stdin.drain()
        proc.stdin.close()
        err_task = asyncio.create_task(_read_head(proc.stderr, 2**16))
        out = await _read_line(proc.stdout, _MAX_STDOUT_BYTES)
        # The result line is in hand, stdout ended, or it passed its cap:
        # stop the process now. Waiting for it to exit would let job code that
        # keeps running (a Worker, a disabled Deno.exit, a closed stdout) hold
        # the slot to the timeout.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        err = await err_task
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(exchange(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"the job ran past its {int(timeout_s)} s limit"}
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if out is None:
        return {"error": "the job's output was too large; nothing was kept"}
    # The exit code doesn't count once the worker printed its result: the
    # process is killed as soon as stdout ends.
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    detail = (err or b"").decode(errors="replace")[-400:]
    logger.warning("python job: sandbox exited %s: %s", proc.returncode, detail)
    return {"error": "the job stopped: it may have run out of memory or crashed"}


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


async def handle_run_python_job(tool_input: dict[str, Any]) -> str:
    from openexecutive.config import get_settings

    if not available():
        return _err("Python jobs aren't available on this server.")
    code = tool_input.get("code")
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
        return _err(f"code must be Python source of at most {MAX_CODE_CHARS} characters")

    files: dict[str, bytes] = {}
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

    started = time.monotonic()
    settings = get_settings()
    body = await run_job(
        code, files, timeout_s=settings.python_job_timeout_s, memory_mb=settings.python_job_memory_mb
    )
    out_files = body.pop("files", None) or {}
    reply: dict[str, Any] = {
        k: v for k, v in body.items() if k in ("result", "error", "printed", "skipped") and v
    }
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
            reply.update(await _keep({n: base64.b64decode(b) for n, b in encoded.items()}))
    # After keeping, so the new files count toward the total.
    await asyncio.to_thread(_prune, jobs_dir())
    reply["seconds"] = round(time.monotonic() - started, 1)
    return json.dumps(reply, ensure_ascii=False)


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
