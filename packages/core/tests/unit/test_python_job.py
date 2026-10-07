"""Python jobs (workflows/python_job.py): Knowledge files and text go in,
result files are kept and served to the principal only. The sandbox itself
is a Deno process; these tests stand in for it, except the last, which runs
the real one when PYTHON_SANDBOX_DIR points at an installed sandbox."""
from __future__ import annotations

import asyncio
import base64
import json
import os
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

    async def fake(code: str, files: dict[str, bytes], *, timeout_s: float) -> dict[str, Any]:
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
    from openexecutive.orchestrator.schedule_tools import PRIVATE_TURN_WITHHELD_TOOLS

    for group in (PRINCIPAL_ONLY_TOOLS, MAIL_TOUCHED_WITHHELD_TOOLS, PRIVATE_TURN_WITHHELD_TOOLS):
        assert python_job.TOOL_NAME in group


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
