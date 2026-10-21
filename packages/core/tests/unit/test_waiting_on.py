"""briefing.waiting_on: the principal's own sent mail nobody has answered."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.briefing import waiting_on

NOW = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
ME = "john@braden.test"


def _sent(mid: str, to: list[str], *, ago: timedelta, thread: str | None = None, **kw: Any) -> Any:
    return SimpleNamespace(
        id=mid, thread_id=thread or f"t-{mid}", from_addr=ME, to=to, cc=[], labels=["SENT"],
        subject=kw.get("subject", f"About {mid}"), received_at=(NOW - ago).isoformat(),
        calendar_invite=False,
    )


def _reply(mid: str) -> Any:
    return SimpleNamespace(id=mid, labels=["INBOX"])


class _Mailbox:
    def __init__(self, sent: list[Any], replied: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.sent = sent
        self.replied = replied
        self.threads_read: list[str] = []

    async def list_sent(self, limit: int = 40) -> list[Any]:
        return self.sent[:limit]

    async def get_thread(self, thread_id: str) -> Any:
        self.threads_read.append(thread_id)
        mine = [m for m in self.sent if m.thread_id == thread_id]
        extra = [_reply(f"r-{thread_id}")] if thread_id in self.replied else []
        return SimpleNamespace(id=thread_id, messages=[*reversed(mine), *extra])


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@braden.test")
    waiting_on.reset_cache()
    yield
    waiting_on.reset_cache()


def _use(monkeypatch: pytest.MonkeyPatch, mailbox: Any) -> None:
    async def _open() -> Any:
        return (mailbox, {ME}) if mailbox is not None else None

    monkeypatch.setattr(waiting_on, "_open_mailbox", _open)


async def test_only_their_unanswered_mail_is_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = _Mailbox(
        [
            _sent("leonard", ["leonard@svn.test"], ago=timedelta(hours=3), subject="SVN numbers"),
            _sent("just-now", ["pat@co.test"], ago=timedelta(minutes=10)),
            _sent("answered", ["andrew@co.test"], ago=timedelta(days=1)),
            _sent("to-exec", ["exec@braden.test"], ago=timedelta(days=1)),
            _sent("crowd", [f"p{i}@co.test" for i in range(11)], ago=timedelta(days=1)),
            _sent("old", ["sam@co.test"], ago=timedelta(days=9)),
        ],
        replied={"t-answered"},
    )
    _use(monkeypatch, mailbox)

    found = await waiting_on.read_waiting(NOW)

    assert found == [waiting_on.Waiting(("leonard@svn.test",), "SVN numbers", NOW - timedelta(hours=3))]
    # Only threads that passed the cheap tests were opened.
    assert mailbox.threads_read == ["t-leonard", "t-answered"]


async def test_a_later_message_of_theirs_in_the_thread_is_the_one_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newer = _sent("second", ["andrew@co.test"], ago=timedelta(hours=2), thread="t-andrew")
    older = _sent("first", ["andrew@co.test"], ago=timedelta(days=2), thread="t-andrew")
    _use(monkeypatch, _Mailbox([newer, older]))

    found = await waiting_on.read_waiting(NOW)

    assert [w.sent_at for w in found or []] == [NOW - timedelta(hours=2)]


async def test_no_mailbox_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, None)
    assert await waiting_on.read_waiting(NOW) is None


async def test_a_failing_mailbox_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken(_Mailbox):
        async def list_sent(self, limit: int = 40) -> list[Any]:
            raise RuntimeError("boom for leonard@svn.test")

    _use(monkeypatch, _Broken([]))
    assert await waiting_on.read_waiting(NOW) is None


async def test_the_read_is_cached_until_it_is_old(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def _read(now: Any = None) -> list[waiting_on.Waiting]:
        calls.append(now)
        return []

    monkeypatch.setattr(waiting_on, "read_waiting", _read)
    assert waiting_on.cached() is None
    await waiting_on.refresh()
    await waiting_on.refresh()
    assert len(calls) == 1 and waiting_on.cached() == []
    await waiting_on.refresh(max_age=0)
    assert len(calls) == 2


async def test_without_act_as_me_the_mailbox_stays_shut(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import gmail
    from openexecutive.delegation import settings as delegation_settings
    from openexecutive.people import store

    principal = SimpleNamespace(id=1, email=ME, email_aliases=[])
    monkeypatch.setattr(store, "find_principal_person", lambda: principal)
    monkeypatch.setattr(delegation_settings, "can_delegate", lambda p: True)
    monkeypatch.setattr(delegation_settings, "is_enabled", lambda pid: False)
    opened: list[str] = []
    monkeypatch.setattr(gmail, "gmail_for", lambda email: opened.append(email))

    assert await waiting_on._open_mailbox() is None
    assert opened == []


def test_turning_act_as_me_off_forgets_the_read(tmp_path: Any) -> None:
    from openexecutive.delegation import settings as delegation_settings

    waiting_on._cache = (0.0, [])
    delegation_settings.set_enabled(1, False, updated_by="test", db_path=tmp_path / "s.db")
    assert waiting_on._cache is None
