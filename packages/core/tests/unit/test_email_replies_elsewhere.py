"""What the Executive already sent a person is shown on their next email,
on any thread and at any of their addresses, and the sender check retries a
read that came back empty."""
from __future__ import annotations

from typing import Any

import openexecutive.integrations.email_poller as poller


def test_what_went_to_the_same_person_on_other_threads_is_shown(tmp_path: Any) -> None:
    from openexecutive.integrations import email_replies

    db = tmp_path / "r.db"
    email_replies.record("email:t1", "sam@example.com", "Forty seats are left.", db_path=db)
    email_replies.record("email:t9", "someone@example.com", "Not for Sam.", db_path=db)
    # A send the Executive made itself, to Sam's other address, from a chat.
    email_replies.record_sent({"to": "Sam <sam@home.example>", "body": "Hello from the Executive."}, db_path=db)
    shown = email_replies.block("email:t2", "sam@example.com", also=["sam@home.example"], db_path=db)
    assert "OTHER THREADS" in shown and "Forty seats are left." in shown and "Hello from the Executive." in shown
    assert "Not for Sam." not in shown and "ON THIS THREAD" not in shown
    # Without the other address, only what went to this one.
    assert "Hello from the Executive." not in email_replies.block("email:t2", "sam@example.com", db_path=db)


def test_one_send_seen_twice_is_kept_once(tmp_path: Any) -> None:
    from openexecutive.integrations import email_replies

    db = tmp_path / "r.db"
    plan = "Here is the plan for the launch week, step by step."
    email_replies.record("email:t1", "sam@example.com", plan, db_path=db)
    # The gateway sees the poller's own send, with the AI line below it.
    email_replies.record_sent(
        {"to": "sam@example.com", "thread_id": "t1", "body": plan + "\n\nWritten by AI."}, db_path=db,
    )
    assert len(email_replies.recent("email:t1", "sam@example.com", db_path=db)) == 1
    email_replies.record_sent({"to": "sam@example.com", "body": "   "}, db_path=db)
    email_replies.record_sent({"body": "No one"}, db_path=db)
    assert email_replies.recent_elsewhere("email:t1", ["sam@example.com"], db_path=db) == []


def test_a_short_send_never_swallows_a_longer_one(tmp_path: Any) -> None:
    from openexecutive.integrations import email_replies

    db = tmp_path / "r.db"
    email_replies.record_sent({"to": "alice@example.com", "body": "Thanks!"}, db_path=db)
    email_replies.record_sent(
        {"to": "alice@example.com", "body": "Thanks! Here are the Q3 numbers you asked for: 12, 14, 19."}, db_path=db,
    )
    assert len(email_replies.recent("sent:alice@example.com", "alice@example.com", db_path=db)) == 2
    # The same long text on another thread is its own send.
    email_replies.record("email:t5", "alice@example.com",
                         "Thanks! Here are the Q3 numbers you asked for: 12, 14, 19.", db_path=db)
    assert len(email_replies.recent("email:t5", "alice@example.com", db_path=db)) == 1


def test_a_tool_send_is_kept_for_each_recipient(tmp_path: Any) -> None:
    from openexecutive.integrations import email_replies

    db = tmp_path / "r.db"
    body = "The launch plan is attached; the first wave goes out Monday."
    email_replies.record_sent(
        {"to": ["Ann <ann@example.com>", "bo@example.com"], "cc": "cy@example.com", "thread_id": "t7", "body": body},
        db_path=db,
    )
    for addr in ("ann@example.com", "bo@example.com", "cy@example.com"):
        assert [b for _w, b in email_replies.recent("email:t7", addr, db_path=db)] == [body]
    # Not shown as "elsewhere" on the thread it went out on.
    assert email_replies.recent_elsewhere("email:t7", ["ann@example.com"], db_path=db) == []
    assert [b for _w, b in email_replies.recent_elsewhere("email:t8", ["ann@example.com"], db_path=db)] == [body]


def test_the_sender_check_retries_only_an_empty_read(monkeypatch: Any) -> None:
    import asyncio as aio

    from openexecutive.integrations import fact_confirmation

    checks: list[str] = []
    waits: list[float] = []

    async def fake_auth(_gw: Any, mid: str, _addr: str) -> bool:
        checks.append(mid)
        return False

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(fact_confirmation, "sender_authenticated", fake_auth)
    monkeypatch.setattr(poller.asyncio, "sleep", fake_sleep)
    for raw, tries in (
        ("", 2),
        ("Error: the mailbox is busy, try again", 2),
        ("Authentication-Results: mx.google.com; dmarc=none", 1),
    ):
        checks.clear()

        async def fake_raw(_gw: Any, _mid: str, raw: str = raw) -> str:
            return raw

        monkeypatch.setattr(fact_confirmation, "read_raw", fake_raw)
        assert aio.run(poller._prove_sender(None, "m1", "a@x.example")) is False
        assert len(checks) == tries
    assert len(waits) == 2
