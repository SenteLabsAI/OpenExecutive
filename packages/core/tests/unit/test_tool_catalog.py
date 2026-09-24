"""The workflow tool catalog: gateway search parsing, exact-name resolution,
save-time availability checks, and the ``oe__read_file`` built-in's
confinement to the download folders."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows import tool_catalog as tc  # noqa: E402
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef  # noqa: E402


def _block(name: str, description: str, schema: dict[str, Any], score: float = 0.8) -> str:
    """One tool block exactly as extensible-mcp's _format_search_results writes it."""
    return "\n".join(
        [
            f"## {name}",
            f"**Description:** {description}",
            "**Parameters:**",
            f"```json\n{json.dumps(schema, indent=2)}\n```",
            f"**Similarity:** {score:.3f}",
            "",
        ]
    )


def _search_text(*blocks: str) -> str:
    return (
        f"Found {len(blocks)} matching tool(s). "
        "Use `call_tool` with the tool name and arguments to invoke one.\n\n" + "\n".join(blocks)
    )


_APPEND_SCHEMA = {
    "type": "object",
    "properties": {"spreadsheet_id": {"type": "string"}, "rows": {"type": "array"}},
    "required": ["spreadsheet_id", "rows"],
}


class _FakeGateway:
    def __init__(self, results: dict[str, str] | None = None, default: str = "") -> None:
        self.results = results or {}
        self.default = default
        self.queries: list[dict[str, Any]] = []

    async def search_tools(self, tool_input: dict[str, Any]) -> str:
        self.queries.append(tool_input)
        return self.results.get(tool_input["query"], self.default)


def _use_gateway(monkeypatch: pytest.MonkeyPatch, gateway: Any) -> None:
    monkeypatch.setattr(tc, "_gateway", lambda: gateway)


# --- parsing ---------------------------------------------------------------


def test_parse_real_search_format() -> None:
    text = _search_text(
        _block("google_workspace__append_table_rows", "Append rows to a table.", _APPEND_SCHEMA),
        _block("google_workspace__read_sheet_values", "Read a range.", {"type": "object"}),
    )
    tools = tc.parse_search_results(text)
    assert [t.name for t in tools] == [
        "google_workspace__append_table_rows",
        "google_workspace__read_sheet_values",
    ]
    assert tools[0].description == "Append rows to a table."
    assert tools[0].input_schema == _APPEND_SCHEMA
    # Name-verb label for the review card: reads are marked, anything else unknown.
    assert tools[0].read_only is None
    assert tools[1].read_only is True
    assert all(t.source == "mcp" for t in tools)


def test_parse_no_results_and_bad_schema() -> None:
    assert tc.parse_search_results("No matching tools found. Try a different search query.") == []
    broken = "## srv__tool\n**Description:** x\n**Parameters:**\n```json\n{not json\n```\n"
    [tool] = tc.parse_search_results(broken)
    assert tool.name == "srv__tool" and tool.input_schema == {}


def test_as_anthropic_tool_falls_back_to_an_object_schema() -> None:
    tool = tc.ToolInfo(name="srv__x", description="d", input_schema={"type": "string"})
    assert tool.as_anthropic_tool()["input_schema"] == {"type": "object", "properties": {}}


# --- resolve / search --------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_keeps_exact_matches_only(monkeypatch: pytest.MonkeyPatch) -> None:
    near_miss = _search_text(
        _block("google_workspace__append_table_rows_v2", "Similar.", _APPEND_SCHEMA),
        _block("google_workspace__append_table_rows", "The one.", _APPEND_SCHEMA),
    )
    gateway = _FakeGateway(
        {"google workspace append table rows": near_miss}, default="No matching tools found."
    )
    _use_gateway(monkeypatch, gateway)
    found = await tc.resolve(
        ["google_workspace__append_table_rows", "srv__missing", "oe__read_file", "oe__nope"]
    )
    assert set(found) == {"google_workspace__append_table_rows", "oe__read_file"}
    assert found["google_workspace__append_table_rows"].description == "The one."
    # Searched by the humanised name with a wide net; an unknown oe__ name never
    # reaches the gateway (built-ins can't be shadowed by an MCP server).
    assert {"query": "google workspace append table rows", "top_k": 10} in gateway.queries
    assert not any("nope" in q["query"] for q in gateway.queries)


@pytest.mark.asyncio
async def test_resolve_without_gateway_returns_builtins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_gateway(monkeypatch, None)
    assert set(await tc.resolve(["oe__message_person", "srv__x"])) == {"oe__message_person"}


@pytest.mark.asyncio
async def test_search_merges_builtins_and_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(
        default=_search_text(_block("fetch__fetch", "Fetch a URL.", {"type": "object"}))
    )
    _use_gateway(monkeypatch, gateway)
    names = [t.name for t in await tc.search("read a downloaded file")]
    assert names[0] == "oe__read_file"
    assert "fetch__fetch" in names


def _defn(tools: list[str]) -> DynamicWorkflowDef:
    return DynamicWorkflowDef.model_validate(
        {
            "name": "file_bills",
            "title": "File bills",
            "steps": [
                {"kind": "action", "id": "file", "title": "File", "goal": "Do it.", "tools": tools},
                {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
            ],
        }
    )


@pytest.mark.asyncio
async def test_validate_tools_available(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(
        {"srv tool": _search_text(_block("srv__tool", "ok", {"type": "object"}))},
        default="No matching tools found.",
    )
    _use_gateway(monkeypatch, gateway)
    assert await tc.validate_tools_available(_defn(["srv__tool", "oe__read_file"])) == []
    [err] = await tc.validate_tools_available(_defn(["srv__tool", "srv__ghost"]))
    assert "srv__ghost" in err and "srv__tool" not in err


@pytest.mark.asyncio
async def test_validate_tools_says_when_gateway_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_gateway(monkeypatch, None)
    [err] = await tc.validate_tools_available(_defn(["srv__tool"]))
    assert "gateway" in err and "not running" in err


# --- oe__read_file -----------------------------------------------------------


def _minimal_pdf(text: str) -> bytes:
    """A tiny single-page PDF with one line of text that pypdf can extract."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


@pytest.fixture()
def download_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "attachments"
    d.mkdir()
    monkeypatch.setenv("WORKFLOW_FILE_DIRS", str(d))
    return d


@pytest.mark.asyncio
async def test_read_file_extracts_pdf_text(download_dir: Path) -> None:
    pdf = download_dir / "bill.pdf"
    pdf.write_bytes(_minimal_pdf("Acme Power invoice total 123.45"))
    out = await tc._read_file({"path": str(pdf)})
    assert "Acme Power invoice total 123.45" in out


@pytest.mark.asyncio
async def test_read_file_reads_text_files(download_dir: Path) -> None:
    (download_dir / "notes.csv").write_text("vendor,amount\nAcme,10\n")
    assert "Acme,10" in await tc._read_file({"path": str(download_dir / "notes.csv")})


@pytest.mark.asyncio
async def test_read_file_refuses_paths_outside_the_download_dirs(
    download_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    for path in (str(secret), str(download_dir / ".." / "secret.txt")):
        out = await tc._read_file({"path": path})
        assert "outside the folders" in out and "TOP SECRET" not in out


@pytest.mark.asyncio
async def test_read_file_refuses_symlink_escape(download_dir: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    link = download_dir / "innocent.txt"
    link.symlink_to(secret)
    out = await tc._read_file({"path": str(link)})
    assert "outside the folders" in out and "TOP SECRET" not in out


@pytest.mark.asyncio
async def test_read_file_rejects_bad_input(download_dir: Path) -> None:
    (download_dir / "run.sh").write_text("echo hi")
    assert "path is required" in await tc._read_file({})
    assert "not found" in await tc._read_file({"path": str(download_dir / "nope.pdf")})
    assert "unsupported file type" in await tc._read_file({"path": str(download_dir / "run.sh")})
    assert "not a file" in await tc._read_file({"path": str(download_dir)})


@pytest.mark.asyncio
async def test_read_file_size_cap(download_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "_MAX_READ_FILE_BYTES", 10)
    (download_dir / "big.txt").write_text("x" * 11)
    assert "larger than" in await tc._read_file({"path": str(download_dir / "big.txt")})


def test_default_download_dir_is_workspace_mcps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_FILE_DIRS", raising=False)
    monkeypatch.delenv("WORKSPACE_ATTACHMENT_DIR", raising=False)
    assert tc._allowed_file_dirs() == [
        (Path.home() / ".workspace-mcp" / "attachments").resolve()
    ]
    monkeypatch.setenv("WORKSPACE_ATTACHMENT_DIR", "/srv/att")
    assert tc._allowed_file_dirs() == [Path("/srv/att").resolve()]
