"""A turn that touched the speaker's own mailbox teaches only from what they
typed, and only into memory that is theirs alone."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.orchestrator import executive


def _session(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(replay=False, private_to_principal=False, **kw)


@pytest.mark.parametrize(("label", "session", "touched", "host", "want"), [
    ("mail turn", _session(), True, False, "reply to Kyle that Friday works"),
    ("not a mail turn", _session(), False, False, ""),
    ("host page", _session(), True, True, ""),
    ("replay", SimpleNamespace(replay=True, private_to_principal=False), True, False, ""),
    ("someone else's mail", SimpleNamespace(replay=False, private_to_principal=True), True, False, ""),
])
def test_what_a_mail_turn_may_teach(label: str, session: Any, touched: bool, host: bool, want: str) -> None:
    got = executive._mail_turn_words(
        session, "reply to Kyle that Friday works", touched_mail=touched, host_words=host,
    )
    assert got == want, label


def test_peer_memory_gets_their_words_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr("openexecutive.memory.honcho_client.sync_turn",
                        lambda *a, **kw: calls.append((a, kw)))
    executive._sync_mail_turn_words("reply to Kyle that Friday works", person_id=1, session_id="s1")
    (args, kw), = calls
    assert args == ("reply to Kyle that Friday works", "")
    assert kw["co_present_person_ids"] is None and kw["person_id"] == 1


def test_both_chat_paths_teach_a_mail_turn_the_same_way() -> None:
    import inspect

    for method in (executive.Executive.stream_chat, executive.Executive.stream_chat_with_committee):
        src = inspect.getsource(method)
        assert "mail_words = _mail_turn_words(" in src, method.__name__
        assert "_sync_mail_turn_words(mail_words" in src, method.__name__
        # Extraction, open loops and style still read speaker_text, which a
        # mail turn leaves empty.
        assert "should_extract(speaker_text" in src, method.__name__
