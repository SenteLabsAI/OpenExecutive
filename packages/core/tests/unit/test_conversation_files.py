"""Files kept for a conversation (orchestrator/conversation_files.py), the
handle parser's edges (orchestrator/file_refs.py) and saved emails read as
text (knowledge/loader.py)."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.knowledge.loader import extract_text_from_eml
from openexecutive.orchestrator import conversation_files, file_refs


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "conversation_files"
    monkeypatch.setattr(conversation_files, "root", lambda: base)
    return base


def _age(folder: Path, days: float) -> None:
    then = time.time() - days * 86400
    os.utime(folder, (then, then))


def test_a_name_with_a_path_stays_inside_its_folder(root: Path) -> None:
    conversation_files.keep("c1", {"../../escape.pdf": b"x"})
    assert conversation_files.names("c1") == ["escape.pdf"]
    assert not (root.parent / "escape.pdf").exists()
    assert conversation_files.get("c1", "../../escape.pdf") == b"x"


def test_prune_drops_old_conversations_then_the_oldest_past_the_cap(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for chat in ("old", "older", "newer", "newest"):
        conversation_files.keep(chat, {"f.bin": b"x" * 100})
    folders = {chat: conversation_files._folder(chat) for chat in ("old", "older", "newer", "newest")}
    _age(folders["old"], conversation_files.KEEP_DAYS + 1)
    _age(folders["older"], 3)
    _age(folders["newer"], 2)
    monkeypatch.setattr(conversation_files, "KEEP_TOTAL_BYTES", 200)
    conversation_files.prune()
    assert not folders["old"].exists()  # past KEEP_DAYS
    assert not folders["older"].exists()  # oldest while over the cap
    assert folders["newer"].exists() and folders["newest"].exists()


def test_keep_sweeps_at_most_every_few_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(conversation_files, "prune", lambda: calls.append(1))
    monkeypatch.setattr(conversation_files, "_last_prune", time.monotonic())
    conversation_files.keep("c1", {"a.pdf": b"x"})
    assert calls == []
    monkeypatch.setattr(conversation_files, "_last_prune", 0.0)
    conversation_files.keep("c1", {"b.pdf": b"x"})
    assert calls == [1]


def test_deleting_a_chat_removes_its_files(tmp_path: Path) -> None:
    from openexecutive.memory import session_store
    from openexecutive.memory.episodic import initialize_db

    db = tmp_path / "s.db"
    initialize_db(db)
    session_store.create_session("gone", "t", "2026-01-01T00:00:00", db_path=db)
    conversation_files.keep("gone", {"a.pdf": b"x"})
    conversation_files.keep("kept", {"b.pdf": b"y"})
    assert session_store.delete_session("gone", db_path=db)
    assert conversation_files.names("gone") == [] and conversation_files.names("kept") == ["b.pdf"]


@pytest.mark.parametrize("bad", ["mail:id#0", "mail:id", "mail:#1", "drive:..", "file:", "x:y"])
def test_bad_handles_are_refused(bad: str) -> None:
    with pytest.raises(file_refs.RefError):
        file_refs.parse(bad)


def test_an_attachment_past_the_last_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation.gmail import MailAttachment

    class Mailbox:
        async def list_attachments(self, message_id: str) -> list[MailAttachment]:
            return [MailAttachment(index=1, name="a.pdf", mime_type="application/pdf", size=1)]

    monkeypatch.setattr("openexecutive.orchestrator.mail_read_tools._id_refusal", lambda *a: None)
    writer = SimpleNamespace(mailbox=Mailbox())
    with pytest.raises(file_refs.RefError, match="has 1 attachment"):
        asyncio.run(file_refs.resolve(["mail:m1#99"], writer=writer))


def test_drive_files_are_the_owners_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_refs, "_principal", lambda: False)
    with pytest.raises(file_refs.RefError, match="Only the owner"):
        asyncio.run(file_refs.resolve(["drive:1AbCdEfGhIjKlMnOp"]))


def _eml(tmp_path: Path, raw: bytes) -> str:
    path = tmp_path / "m.eml"
    path.write_bytes(raw)
    return extract_text_from_eml(path)


def test_a_saved_email_reads_html_only_bad_charset_and_no_body(tmp_path: Path) -> None:
    head = b"From: a@co.example\r\nSubject: Hi\r\nMIME-Version: 1.0\r\n"
    html = _eml(tmp_path, head + b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>Signed <b>both</b></p>")
    assert "Subject: Hi" in html and "Signed" in html and "<p>" not in html
    odd = _eml(tmp_path, head + b"Content-Type: text/plain; charset=x-made-up\r\n\r\nstill here")
    assert "Subject: Hi" in odd
    empty = _eml(tmp_path, head + b"\r\n")
    assert empty.startswith("From: a@co.example")
