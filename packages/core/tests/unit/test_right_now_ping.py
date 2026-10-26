"""briefing.right_now_ping: a chat message when Right now gains a point."""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.audit.logger import AuditLogger, set_audit_logger
from openexecutive.briefing import right_now_ping
from openexecutive.briefing.right_now import point_key
from openexecutive.people import notifications
from openexecutive.people.notifications import NotificationSettings

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)
ANDY = "**Andy Agee** — replied on the loan figures and wants a call."
BOARD = "**Board deck** — the draft is in."


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from openexecutive.memory import workspace_settings
    from openexecutive.people import store
    from openexecutive.scheduler import runner

    logger = AuditLogger(db_path=tmp_path / "audit.db")
    set_audit_logger(logger)
    monkeypatch.setattr(workspace_settings, "get_user_timezone", lambda *a, **k: UTC)
    state: dict[str, Any] = {"settings": NotificationSettings(), "sent": [], "hold": None, "ok": True}
    me = SimpleNamespace(id=1, full_name="Pat Lee")
    monkeypatch.setattr(store, "find_principal_person", lambda: me)
    monkeypatch.setattr(notifications, "get_notification_settings", lambda pid: state["settings"])
    monkeypatch.setattr(notifications, "hold_for", lambda pid, **kw: state["hold"])

    async def _deliver(person: Any, text: str, *, label: str = "", chat_only: bool = False) -> Any:
        state["sent"].append((text, label, chat_only))
        if not state["ok"]:
            return runner.PrincipalDelivery(False, "none", "no_channel")
        return runner.PrincipalDelivery(True, "42", "delivered", "telegram")

    monkeypatch.setattr(runner, "deliver_to_person", _deliver)
    yield state
    set_audit_logger(None)


async def test_a_new_point_that_needs_them_is_sent_on_their_chat_app(env: dict[str, Any]) -> None:
    result = await right_now_ping.maybe_ping([ANDY, BOARD], {point_key(ANDY)}, earlier_today=True, now=NOW)

    assert result.status == "sent" and result.channel == "telegram"
    [(text, label, chat_only)] = env["sent"]
    assert text == "Right now: Andy Agee — replied on the loan figures and wants a call."
    assert label == "Right now" and chat_only is True


async def test_everything_sends_every_new_point_in_one_message(env: dict[str, Any]) -> None:
    env["settings"] = NotificationSettings(right_now="everything")
    await right_now_ping.maybe_ping([ANDY, BOARD], set(), earlier_today=True, now=NOW)

    [(text, _, _)] = env["sent"]
    assert text.startswith("Right now:\n• Andy Agee") and "• Board deck" in text


@pytest.mark.parametrize(
    ("setup", "important", "earlier", "status"),
    [
        (lambda e: e.update(settings=NotificationSettings(right_now="off")), {"andy agee"}, True, "off"),
        (lambda e: None, {"andy agee"}, False, "first_today"),
        (lambda e: None, set(), True, "not_important"),
        (lambda e: e.update(hold=notifications.Hold(None, "quiet")), {"andy agee"}, True, "quiet"),
        (lambda e: e.update(ok=False), {"andy agee"}, True, "no_chat_app"),
    ],
)
async def test_when_nothing_is_sent(
    env: dict[str, Any], setup: Any, important: set[str], earlier: bool, status: str,
) -> None:
    setup(env)
    result = await right_now_ping.maybe_ping([ANDY], important, earlier_today=earlier, now=NOW)

    assert result.status == status
    if status != "no_chat_app":
        assert env["sent"] == []  # dropped, not held: Home and the brief say it later


async def test_a_point_that_is_only_a_link_sends_nothing(env: dict[str, Any]) -> None:
    bare = "<https://evil.xyz/x> bob@corp.com"
    assert right_now_ping.ping_text([bare]) == ""
    result = await right_now_ping.maybe_ping([bare], {point_key(bare)}, earlier_today=True, now=NOW)

    assert result.status == "nothing_left" and env["sent"] == []
    # Mixed with a real point, it is left out and the rest goes.
    assert right_now_ping.ping_text([bare, ANDY]).startswith("Right now: Andy Agee")


async def test_at_most_four_a_day(env: dict[str, Any]) -> None:
    for i in range(right_now_ping.DAILY_MAX):
        point = f"**Item {i}** — new."
        assert (await right_now_ping.maybe_ping([point], {point_key(point)}, earlier_today=True, now=NOW)).status == "sent"
    late = "**One more** — new."
    result = await right_now_ping.maybe_ping([late], {point_key(late)}, earlier_today=True, now=NOW)

    assert result.status == "daily_limit" and len(env["sent"]) == right_now_ping.DAILY_MAX


def test_a_point_goes_out_with_no_link_or_address_in_it() -> None:
    text = (
        "**Wire request** — [verify here](https://evil.example/x) or mail pay@evil.example, "
        "see evil.com <https://x.test|now> www.bad.test"
    )
    out = right_now_ping.plain_point(text)

    assert "http" not in out and "@" not in out and "evil" not in out and "bad.test" not in out
    assert out.startswith("Wire request — verify here")
    assert "<" not in out and "*" not in out


@pytest.mark.parametrize(
    "bad",
    [
        "go to evil.ru/login", "Visit evil.xyz now", "bit.ly/abc", "discord.gg/abc", "evil.zip",
        "evil.ca/pay", "xn--pple-43d.shop/login", "ftp://evil.xyz", "tg://resolve?domain=x",
        "mailto:x@y", "1.2.3.4/login", "evil.museum", "paypal-secure.com.evil.top",
        "evil．com", "ＷＷＷ.evil.top", "https://x.test",
        "evil\u200b.com", "evil.\u200bcom/pay", "evil\u00ad.top", "evil.\u2060top",
    ],
)
def test_nothing_link_like_survives(bad: str) -> None:
    out = right_now_ping.plain_point(f"**Payroll** — re-verify at {bad} today.")

    assert out.startswith("Payroll — re-verify at")
    assert not re.search(r"[\w-]\.[^\W\d_]{2,}|\d\.\d|:|@", out), out


def test_ordinary_words_and_figures_stay() -> None:
    out = right_now_ping.plain_point("**St. Albans** — offer is $4.6M, due 3 p.m.; e.g. Pine View")
    assert out == "St. Albans — offer is $4.6M, due 3 p.m.; e.g. Pine View"


def test_a_bold_label_with_a_colon_is_kept() -> None:
    assert right_now_ping.plain_point("**Andy Agee:** wants a call at 3:30.") == "Andy Agee: wants a call at 3:30."
