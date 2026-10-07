"""Python jobs: library-heavy work on files (split a PDF, build a spreadsheet,
draw a chart) in a WebAssembly sandbox.

``run_script`` (Monty) decides what to do and calls the Executive's tools;
it can't run libraries. ``run_python_job`` is the workshop for that: Python
in Pyodide (WebAssembly) with a fixed, bundled set of libraries (pypdf,
openpyxl, pandas, numpy, matplotlib, Pillow, lxml), run by Deno in a process
of its own per job.

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
import json
import logging
import re
import shutil
import time
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
_KEEP_DAYS = 7
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
        "(matplotlib), work with images (Pillow). Input files are under /in, named "
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


async def run_job(code: str, files: dict[str, bytes], *, timeout_s: float) -> dict[str, Any]:
    """Run one job in a fresh sandbox process. Never raises (cancellation
    aside, which kills the process)."""
    base = _sandbox_dir()
    wheels = sorted(str(p) for p in (base / "wheels").glob("*.whl"))
    job = json.dumps({
        "code": code,
        "pyodide": str(base / "pyodide"),
        "wheels": wheels,
        "files": {n: base64.b64encode(b).decode() for n, b in files.items()},
    }).encode()
    async with _slot:
        proc = await asyncio.create_subprocess_exec(
            str(base / "deno"), "run", "--no-prompt", "--quiet", "--no-remote",
            f"--allow-read={base},{_WORKER}", str(_WORKER),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Nothing of the server's environment: no keys, no proxies.
            env={"PATH": "/usr/bin:/bin", "HOME": str(base), "DENO_NO_UPDATE_CHECK": "1",
                 "DENO_DIR": str(jobs_dir() / ".deno")},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(job), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": f"the job ran past its {int(timeout_s)} s limit"}
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
    if proc.returncode != 0:
        logger.warning("python job: sandbox exited %s: %s", proc.returncode, err.decode()[-400:])
        return {"error": "the sandbox failed to run the job"}
    try:
        body = json.loads(out)
    except ValueError:
        return {"error": "the sandbox returned no result"}
    return body if isinstance(body, dict) else {"error": "the sandbox returned no result"}


def _knowledge_file(name: str) -> Path | None:
    from openexecutive.orchestrator.document_tools import _company_docs_dir

    if not _SAFE_NAME.match(name):
        return None
    docs = _company_docs_dir()
    path = (docs / name).resolve()
    return path if path.is_file() and path.is_relative_to(docs) else None


def _prune(root: Path) -> None:
    cutoff = time.time() - _KEEP_DAYS * 86400
    for child in root.iterdir() if root.is_dir() else []:
        if _JOB_ID.match(child.name) and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


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
    body = await run_job(code, files, timeout_s=get_settings().python_job_timeout_s)
    out_files = body.pop("files", None) or {}
    reply: dict[str, Any] = {
        k: v for k, v in body.items() if k in ("result", "error", "printed") and v
    }
    if out_files:
        if len(out_files) > _MAX_OUTPUT_FILES:
            reply["files_error"] = f"the job wrote more than {_MAX_OUTPUT_FILES} files; none kept"
        else:
            decoded = {n: base64.b64decode(b) for n, b in out_files.items() if _SAFE_NAME.match(n)}
            if sum(map(len, decoded.values())) > _MAX_OUTPUT_BYTES:
                reply["files_error"] = "the result files are too large to keep"
            else:
                root = jobs_dir()
                job_id = uuid.uuid4().hex
                folder = root / job_id
                await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
                for name, data in decoded.items():
                    await asyncio.to_thread((folder / name).write_bytes, data)
                await asyncio.to_thread(_prune, root)
                reply["files"] = [
                    {"name": n, "bytes": len(d), "link": f"/api/backend/python-jobs/{job_id}/{n}"}
                    for n, d in sorted(decoded.items())
                ]
                reply["note"] = "Give the person each file's link as a markdown link."
    reply["seconds"] = round(time.monotonic() - started, 1)
    return json.dumps(reply, ensure_ascii=False)


def result_file(job_id: str, name: str) -> Path | None:
    """A kept result file, or None (bad id or name, or gone)."""
    if not _JOB_ID.match(job_id) or not _SAFE_NAME.match(name):
        return None
    root = jobs_dir().resolve()
    path = (root / job_id / name).resolve()
    return path if path.is_file() and path.is_relative_to(root) else None


PYTHON_JOB_TOOLS: list[dict[str, Any]] = [TOOL_DEFINITION]
PYTHON_JOB_TOOL_HANDLERS = {TOOL_NAME: handle_run_python_job}
