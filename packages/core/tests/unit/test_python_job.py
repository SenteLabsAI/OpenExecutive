"""Python jobs (workflows/python_job.py): Knowledge files and text go in,
result files are kept and served to the principal only. The sandbox itself
is a Deno process; these tests stand in for it, except the last, which runs
the real one when PYTHON_SANDBOX_DIR points at an installed sandbox."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.api.routes import python_jobs as route  # noqa: E402
from openexecutive.workflows import python_job  # noqa: E402


@pytest.fixture()
def company(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bundle.pdf").write_bytes(b"%PDF-1.4 test")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "profile.yaml"))
    monkeypatch.setattr(python_job, "available", lambda: True)
    return tmp_path


@pytest.fixture()
def sandbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stands in for the Deno process: records each job, writes one file."""
    jobs: list[dict[str, Any]] = []

    async def fake(code: str, files: dict[str, bytes], *, timeout_s: float, **_: Any) -> dict[str, Any]:
        jobs.append({"code": code, "files": files})
        return {"result": "ok", "files": {"out.txt": base64.b64encode(b"done").decode()}}

    monkeypatch.setattr(python_job, "run_job", fake)
    return jobs


def _run(tool_input: dict[str, Any]) -> dict[str, Any]:
    return json.loads(asyncio.run(python_job.handle_run_python_job(tool_input)))


def test_knowledge_and_text_files_go_in_and_results_get_links(company: Path, sandbox: list) -> None:
    out = _run({"code": "1", "knowledge_files": ["bundle.pdf"], "text_files": {"a.csv": "x,y\n1,2\n"}})
    assert sandbox[0]["files"] == {"bundle.pdf": b"%PDF-1.4 test", "a.csv": b"x,y\n1,2\n"}
    [f] = out["files"]
    assert f["name"] == "out.txt" and f["link"].startswith("/api/backend/python-jobs/")
    job_id, name = f["link"].split("/")[-2:]
    assert python_job.result_file(job_id, name).read_bytes() == b"done"  # type: ignore[union-attr]


@pytest.mark.parametrize("name", ["../profile.yaml", "missing.pdf", "/etc/passwd", "docs/../x"])
def test_only_knowledge_files_by_plain_name(company: Path, sandbox: list, name: str) -> None:
    assert "no Knowledge file" in _run({"code": "1", "knowledge_files": [name]})["error"]
    assert sandbox == []


def test_bad_inputs_are_refused(company: Path, sandbox: list) -> None:
    assert "code must be" in _run({"code": ""})["error"]
    assert "bad text file" in _run({"code": "1", "text_files": {"../x": "y"}})["error"]
    assert sandbox == []


def test_unavailable_without_the_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHON_SANDBOX_DIR", str(tmp_path / "nothing"))
    assert python_job.available() is False
    assert "aren't available" in _run({"code": "1"})["error"]


def test_result_files_need_a_real_id_and_name(company: Path) -> None:
    assert python_job.result_file("not-an-id", "x") is None
    assert python_job.result_file("0" * 32, "../../profile.yaml") is None


def test_the_download_route_is_the_principals(company: Path, sandbox: list, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run({"code": "1"})
    job_id, name = out["files"][0]["link"].split("/")[-2:]
    owner = {"is": True}
    monkeypatch.setattr("openexecutive.api.routes.people.caller_is_principal", lambda _r: owner["is"])
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    resp = client.get(f"/python-jobs/{job_id}/{name}")
    assert resp.status_code == 200 and resp.content == b"done"
    assert resp.headers["content-security-policy"] == "sandbox"
    assert "attachment" in resp.headers["content-disposition"]
    assert client.get(f"/python-jobs/{job_id}/nope.txt").status_code == 404
    owner["is"] = False
    assert client.get(f"/python-jobs/{job_id}/{name}").status_code == 403


def test_the_tool_is_principal_only_and_withheld_where_scripts_are() -> None:
    from openexecutive.delegation.lockdown import MAIL_TOUCHED_WITHHELD_TOOLS
    from openexecutive.orchestrator.content_trust import PRINCIPAL_ONLY_TOOLS
    from openexecutive.orchestrator.schedule_tools import (
        PRIVATE_TURN_WITHHELD_TOOLS,
        UNATTENDED_WITHHELD_TOOLS,
    )

    for group in (
        PRINCIPAL_ONLY_TOOLS, MAIL_TOUCHED_WITHHELD_TOOLS, PRIVATE_TURN_WITHHELD_TOOLS,
        UNATTENDED_WITHHELD_TOOLS,
    ):
        assert python_job.TOOL_NAME in group


def test_oversized_results_are_refused_before_decoding(company: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = "A" * (python_job._MAX_OUTPUT_BYTES * 4 // 3 + 100)

    async def fake(code: str, files: dict[str, bytes], **_: Any) -> dict[str, Any]:
        return {"result": "ok", "files": {"big.bin": big}}

    monkeypatch.setattr(python_job, "run_job", fake)
    decoded: list[Any] = []
    monkeypatch.setattr(python_job.base64, "b64decode", lambda *a, **k: decoded.append(a) or b"")
    out = _run({"code": "1"})
    assert "too many or too large" in out["files_error"] and decoded == []


def test_links_are_url_encoded(company: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(code: str, files: dict[str, bytes], **_: Any) -> dict[str, Any]:
        return {"files": {"My report (v2).pdf": base64.b64encode(b"x").decode()}}

    monkeypatch.setattr(python_job, "run_job", fake)
    [f] = _run({"code": "1"})["files"]
    assert f["link"].endswith("/My%20report%20%28v2%29.pdf")


async def test_the_api_stops_reading_a_flood(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 300)
    reader.feed_eof()
    assert await python_job._read_line(reader, 100) is None
    reader = asyncio.StreamReader()
    reader.feed_data(b"ok")
    reader.feed_eof()
    assert await python_job._read_line(reader, 100) == b"ok"
    # Up to the result line only: the process may never close stdout.
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"result": "ok"}\nmore')
    assert await python_job._read_line(reader, 100) == b'{"result": "ok"}'


@pytest.mark.skipif(
    not (Path(os.environ.get("PYTHON_SANDBOX_DIR", "/opt/pysandbox")) / "deno").is_file(),
    reason="the sandbox is installed by the API image",
)
def test_the_real_sandbox_runs_a_job_and_keeps_the_server_out(monkeypatch: pytest.MonkeyPatch) -> None:
    code = "import os\nopen('/out/r.txt','w').write(str(sorted(os.environ)))\n'ok'"
    body = asyncio.run(python_job.run_job(code, {}, timeout_s=120))
    assert body["result"] == "ok"
    # Pyodide's own environment only: none of the server's variables.
    assert "ANTHROPIC_API_KEY" not in base64.b64decode(body["files"]["r.txt"]).decode()
    escape = "from pyodide.code import run_js\nrun_js(\"Deno.env.get('PATH')\")"
    assert "NotCapable" in asyncio.run(python_job.run_job(escape, {}, timeout_s=120))["error"]
    # A result file past the cap is named, not sent.
    huge = f"open('/out/huge.bin','wb').write(b'x' * {python_job._MAX_OUTPUT_BYTES + 1})\n'ok'"
    body = asyncio.run(python_job.run_job(huge, {}, timeout_s=120))
    assert body["skipped"] == ["huge.bin"] and body["files"] == {}
    # Memory past the caps fails the job, not the server.
    hog = "from pyodide.code import run_js\nrun_js('let a=[]; for(let i=0;i<8;i++){const b=new Uint8Array(2**30); b.fill(1); a.push(b);} a.length')"
    assert "allocation failed" in asyncio.run(python_job.run_job(hog, {}, timeout_s=120))["error"]


@pytest.mark.skipif(
    not (Path(os.environ.get("PYTHON_SANDBOX_DIR", "/opt/pysandbox")) / "deno").is_file(),
    reason="the sandbox is installed by the API image",
)
def test_the_real_sandbox_makes_documents_from_scratch() -> None:
    # docx, pptx and fpdf need Pyodide packages (lxml, Pillow, fonttools)
    # that Pyodide can't infer from these imports; the worker's NEEDS loads them.
    code = (
        "from docx import Document\nd = Document(); d.add_heading('Plan', 0); d.save('/out/plan.docx')\n"
        "from pptx import Presentation\np = Presentation(); p.slides.add_slide(p.slide_layouts[0]); p.save('/out/deck.pptx')\n"
        "from fpdf import FPDF\nf = FPDF(); f.add_page(); f.set_font('Helvetica', size=12); f.cell(text='Hi'); f.output('/out/r.pdf')\n"
        "'ok'"
    )
    body = asyncio.run(python_job.run_job(code, {}, timeout_s=120))
    assert body["error"] is None and body["result"] == "ok"
    files = {name: base64.b64decode(data) for name, data in body["files"].items()}
    assert files["plan.docx"][:2] == b"PK" and files["deck.pptx"][:2] == b"PK"
    assert files["r.pdf"].startswith(b"%PDF")


def test_kept_results_are_capped_oldest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(python_job, "_KEEP_TOTAL_BYTES", 250)
    now = time.time()
    for i, age_days in enumerate((8, 3, 2, 1)):
        job = tmp_path / f"{i:032x}"
        job.mkdir()
        (job / "r.bin").write_bytes(b"x" * 100)
        os.utime(job, (now - age_days * 86400,) * 2)
    (tmp_path / ".deno").mkdir()
    python_job._prune(tmp_path)
    # Past its days, then the oldest until the rest fit; Deno's old cache too.
    assert sorted(p.name[-1] for p in tmp_path.iterdir()) == ["2", "3"]


_REAL = pytest.mark.skipif(
    not (Path(os.environ.get("PYTHON_SANDBOX_DIR", "/opt/pysandbox")) / "deno").is_file(),
    reason="the sandbox is installed by the API image",
)


@_REAL
def test_the_real_sandbox_leaves_nothing_behind_and_stops_early(company: Path) -> None:
    # Modules a job imports fed Deno's cache on the company volume, which was
    # never cleaned up.
    feed = (
        "from pyodide.code import run_js\n"
        "run_js(\"(async()=>{for(let i=0;i<3;i++) await import('data:text/javascript,export const x=\\\"'"
        "+'B'.repeat(2**20)+Math.random()+'\\\"')})()\")\n'ok'"
    )
    assert asyncio.run(python_job.run_job(feed, {}, timeout_s=60))["result"] == "ok"
    assert not python_job.jobs_dir().exists()
    # A flood is stopped as soon as it passes the cap, not at the timeout.
    flood = "from pyodide.code import run_js\nrun_js(\"console.log('o'.repeat(40*2**20))\")"
    started = time.monotonic()
    body = asyncio.run(python_job.run_job(flood, {}, timeout_s=60))
    assert body["error"] == "the job's output was too large; nothing was kept"
    assert time.monotonic() - started < 30
    # A Worker left running doesn't hold the job open after its result.
    worker = (
        "from pyodide.code import run_js\n"
        "run_js(\"new Worker('data:text/javascript,for(;;){}', {type: 'module'})\")\n'ok'"
    )
    started = time.monotonic()
    assert asyncio.run(python_job.run_job(worker, {}, timeout_s=60))["result"] == "ok"
    assert time.monotonic() - started < 30
    # Nor one the job stops the worker's own exit for, or a closed stdout.
    for trick in (
        "Deno.exit=()=>{}; new Worker('data:text/javascript,for(;;){}', {type: 'module'})",
        "setTimeout(()=>{Deno.stdout.close(); for(;;){}}, 0)",
    ):
        started = time.monotonic()
        body = asyncio.run(python_job.run_job(
            f"from pyodide.code import run_js\nrun_js({trick!r})\n'ok'", {}, timeout_s=60
        ))
        assert time.monotonic() - started < 30, (trick, body)


def test_concurrent_prunes_dont_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(python_job, "_KEEP_TOTAL_BYTES", 0)
    for i in range(300):
        job = tmp_path / f"{i:032x}"
        job.mkdir()
        (job / "r.bin").write_bytes(b"x")
    errors: list[BaseException] = []

    def prune() -> None:
        try:
            python_job._prune(tmp_path)
        except BaseException as exc:  # noqa: BLE001 - the test records any failure
            errors.append(exc)

    threads = [threading.Thread(target=prune) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and list(tmp_path.iterdir()) == []
