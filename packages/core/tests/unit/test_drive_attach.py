"""Drive files on a draft written as the person (orchestrator/drive_attach.py,
``ghostwrite_email``'s ``drive_files``)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openexecutive.delegation import saved_files
from openexecutive.orchestrator import drive_attach as da
from openexecutive.orchestrator import mail_file_save, mcp_gateway
from openexecutive.workflows import tool_catalog

FILE_A = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
FILE_B = "1ZyXwVuTsRqPoNmLkJiHgFeDcBa543210"
UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class FakeGateway:
    """Saves each requested file into ``folder``, as workspace-mcp does, and
    answers in its words."""

    def __init__(
        self, folder: Path, contents: dict[str, bytes], *,
        saved_to: Path | None = None, drive_name: str | None = None, declared: dict[str, int] | None = None,
    ) -> None:
        self.folder = folder
        self.contents = contents
        self.saved_to = saved_to
        self.drive_name = drive_name
        self.declared = declared or {}
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        self.calls.append(tool_input)
        # Only the code-built object itself gets through the mail lockdown.
        assert mcp_gateway._code_built_admits(tool_input)
        file_id = tool_input["arguments"]["file_id"]
        if file_id not in self.contents:
            return "Error: file not found"
        name = self.drive_name or f"scan-{file_id[:4]}.pdf"
        if tool_input["name"] == da.INFO_TOOL:
            size = self.declared.get(file_id, len(self.contents[file_id]))
            return f"File: {name}\nID: {file_id}\nType: application/pdf\nSize: {size} bytes\nCreated: N/A"
        path = self.saved_to or self.folder / f"scan-{file_id[:4]}_{UUID}.pdf"
        path.write_bytes(self.contents[file_id])
        return (
            f"File downloaded successfully!\nFile: {name}\nFile ID: {file_id}\n"
            f"Size: 1 KB\nMIME Type: application/pdf\n\n\U0001f4ce Saved to: {path}\n\nThe file has been saved."
        )

    def downloads(self) -> list[str]:
        return [c["arguments"]["file_id"] for c in self.calls if c["name"] == da.DOWNLOAD_TOOL]


@pytest.fixture
def downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "attachments"
    folder.mkdir()
    monkeypatch.setattr(tool_catalog, "_allowed_file_dirs", lambda: [folder.resolve()])
    monkeypatch.setattr(mail_file_save, "_exec_account", lambda: "exec@co.example")
    monkeypatch.setattr(mcp_gateway, "gateway_server_names", lambda _g: ["google_workspace"])
    monkeypatch.setattr(mcp_gateway, "gateway_reconnect_needed", lambda _g: [])
    monkeypatch.setattr(saved_files, "protected", lambda _viewer: (frozenset(), frozenset()))
    return folder


def _use(monkeypatch: pytest.MonkeyPatch, gateway: Any) -> None:
    monkeypatch.setattr(mcp_gateway, "get_active_gateway", lambda: gateway)


def test_ids_are_checked_and_deduplicated() -> None:
    assert da.valid_ids([FILE_A, FILE_A, FILE_B]) == [FILE_A, FILE_B]
    assert "Not a Drive file id" in str(da.valid_ids(["../etc/passwd"]))
    assert "must list" in str(da.valid_ids([]))
    assert "At most" in str(da.valid_ids([f"{FILE_A[:-2]}{i:02d}" for i in range(11)]))


def test_files_come_back_as_bytes_and_the_copies_are_removed(
    downloads: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway(downloads, {FILE_A: b"%PDF-a", FILE_B: b"%PDF-b"})
    _use(monkeypatch, gateway)
    got = asyncio.run(da.fetch([FILE_A, FILE_B], viewer=1))
    assert [(n, m, d) for n, m, d in got.files] == [
        (f"scan-{FILE_A[:4]}.pdf", "application/pdf", b"%PDF-a"),
        (f"scan-{FILE_B[:4]}.pdf", "application/pdf", b"%PDF-b"),
    ]
    assert got.skipped == 0
    assert list(downloads.iterdir()) == []
    assert [c["name"] for c in gateway.calls[:2]] == [da.INFO_TOOL, da.DOWNLOAD_TOOL]
    assert gateway.calls[1]["arguments"]["user_google_email"] == "exec@co.example"


def test_files_past_the_cap_are_left_off(downloads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import gmail

    monkeypatch.setattr(gmail, "FORWARD_MAX_BYTES", 8)
    gateway = FakeGateway(downloads, {FILE_A: b"123456", FILE_B: b"123456"})
    _use(monkeypatch, gateway)
    got = asyncio.run(da.fetch([FILE_A, FILE_B], viewer=1))
    assert len(got.files) == 1 and got.skipped == 1
    # Drive said it was too large, so it was never downloaded.
    assert gateway.downloads() == [FILE_A]
    assert list(downloads.iterdir()) == []


def test_someone_elses_saved_file_is_refused(downloads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway(downloads, {FILE_A: b"x"})
    _use(monkeypatch, gateway)
    monkeypatch.setattr(saved_files, "protected", lambda _viewer: (frozenset({FILE_A}), frozenset()))
    with pytest.raises(da.AttachError, match="isn't one I can attach"):
        asyncio.run(da.fetch([FILE_A], viewer=2))
    monkeypatch.setattr(saved_files, "protected", lambda _viewer: (frozenset(), frozenset({FILE_A})))
    with pytest.raises(da.AttachError, match="isn't one I can attach"):
        asyncio.run(da.fetch([FILE_A], viewer=1))
    assert gateway.calls == []


def test_an_unreadable_saved_files_record_fails_closed(downloads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeGateway(downloads, {FILE_A: b"x"}))

    def broken(_viewer: Any) -> tuple[frozenset[str], frozenset[str]]:
        raise RuntimeError("db locked")

    monkeypatch.setattr(saved_files, "protected", broken)
    with pytest.raises(da.AttachError, match="belong to"):
        asyncio.run(da.fetch([FILE_A], viewer=1))


def test_a_file_saved_outside_the_download_folders_is_not_read(
    downloads: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / f"secret_{UUID}.pdf"
    _use(monkeypatch, FakeGateway(downloads, {FILE_A: b"x"}, saved_to=elsewhere))
    with pytest.raises(da.AttachError, match="don't read from"):
        asyncio.run(da.fetch([FILE_A], viewer=1))


def test_no_drive_and_a_failed_download_are_errors(downloads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, None)
    with pytest.raises(da.AttachError, match="isn't connected"):
        asyncio.run(da.fetch([FILE_A], viewer=1))
    _use(monkeypatch, FakeGateway(downloads, {}))
    with pytest.raises(da.AttachError, match="Couldn't get"):
        asyncio.run(da.fetch([FILE_A], viewer=1))


def test_a_drive_name_cannot_point_at_another_saved_file(
    downloads: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer echoes the Drive name, which whoever shared the file chose,
    before workspace-mcp's own Saved to line."""
    other = downloads / f"payroll_{UUID.replace('0a1', '2e6')}.pdf"
    other.write_bytes(b"secret")
    # On the same line: not anchored, so it is not a Saved to line at all.
    inline = FakeGateway(downloads, {FILE_A: b"mine"}, drive_name=f"Q3.pdf Saved to: {other}")
    _use(monkeypatch, inline)
    got = asyncio.run(da.fetch([FILE_A], viewer=1))
    assert got.files[0][2] == b"mine" and got.files[0][0] == f"scan-{FILE_A[:4]}.pdf"
    # On a line of its own: two Saved to lines, so neither is trusted.
    _use(monkeypatch, FakeGateway(downloads, {FILE_A: b"mine"}, drive_name=f"Q3.pdf\n\U0001f4ce Saved to: {other}"))
    with pytest.raises(da.AttachError, match="didn't hand"):
        asyncio.run(da.fetch([FILE_A], viewer=1))
    assert other.read_bytes() == b"secret"


def test_a_saved_name_without_workspace_mcps_uuid_is_refused(
    downloads: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = downloads / "notes.pdf"
    _use(monkeypatch, FakeGateway(downloads, {FILE_A: b"x"}, saved_to=plain))
    with pytest.raises(da.AttachError, match="didn't hand"):
        asyncio.run(da.fetch([FILE_A], viewer=1))
