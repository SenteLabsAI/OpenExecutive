"""Unit tests for the two new Executive tools in Phase G:

- `lookup_person` — case-insensitive substring match on people, returns up
  to 5 hits with full routing identifiers.
- `send_discord_dm` — outbound DM via Discord REST API; mirrors
  `send_slack_dm` (validation, audit, structured error JSON).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.orchestrator.schedule_tools import (
    handle_lookup_person,
    handle_send_discord_dm,
)
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


def _call(coro_fn, payload: dict) -> dict:
    return json.loads(asyncio.run(coro_fn(payload)))


# --------------------------------------------------------------------------- #
# lookup_person
# --------------------------------------------------------------------------- #


def test_lookup_person_finds_by_name_substring() -> None:
    people_store.upsert_person(full_name="Sarah Chen", role="CFO", email="sarah@x.com")
    people_store.upsert_person(full_name="Devon Park", role="CTO")
    people_store.upsert_person(full_name="Mia Rodriguez", role="Head of Sales")

    result = _call(handle_lookup_person, {"query": "sara"})

    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["full_name"] == "Sarah Chen"
    assert match["role"] == "CFO"
    assert match["email"] == "sarah@x.com"
    # All routing identifiers must be present in the response shape, even
    # when null — the Executive should never have to guess which fields
    # were omitted vs unset.
    for key in ("slack_user_id", "telegram_chat_id", "discord_user_id"):
        assert key in match


def test_lookup_person_finds_by_role_substring() -> None:
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")
    people_store.upsert_person(full_name="Devon Park", role="CTO")
    people_store.upsert_person(full_name="Mia Rodriguez", role="Head of Sales")

    result = _call(handle_lookup_person, {"query": "sales"})

    assert len(result["matches"]) == 1
    assert result["matches"][0]["full_name"] == "Mia Rodriguez"


def test_lookup_person_returns_discord_user_id_when_set() -> None:
    people_store.upsert_person(
        full_name="Sarah Chen",
        role="CFO",
        discord_user_id="111222333444555666",
    )

    result = _call(handle_lookup_person, {"query": "sarah"})

    assert result["matches"][0]["discord_user_id"] == "111222333444555666"


def test_lookup_person_uses_person_id_not_bare_id() -> None:
    """The internal roster reference must be exposed as `person_id`, never a
    bare `id` — a bare `id` was being conflated by the Executive with a
    channel snowflake and passed straight into send_discord_dm (regression:
    'send_discord_dm: refused user_id=137 (not in People roster)')."""
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")

    match = _call(handle_lookup_person, {"query": "sarah"})["matches"][0]

    assert "person_id" in match
    assert "id" not in match


def test_send_discord_dm_recovers_when_caller_passes_person_id() -> None:
    """The Executive keeps passing a Person id (== its Honcho peer id) instead
    of the Discord snowflake. When that id resolves to a rostered person who
    HAS a Discord id, the handler must route to their real Discord id rather
    than refuse — that person is the intended recipient."""
    pid = people_store.upsert_person(
        full_name="Dana Ops", discord_user_id="123456789012345678"
    )
    fake_send = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings") as mock_get,
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        mock_get.return_value.discord_bot_token = "fake-token"
        # Pass the person_id (a small int), NOT the snowflake.
        result = _call(handle_send_discord_dm, {"discord_user_id": str(pid), "text": "hi"})
    assert result["status"] == "sent"
    # Sent to the resolved snowflake, not the person_id that was passed.
    fake_send.assert_awaited_once_with("123456789012345678", "hi")
    assert result["discord_user_id"] == "123456789012345678"


def test_send_discord_dm_person_id_without_discord_still_refused() -> None:
    """A person_id that resolves to someone with NO Discord id is not
    recoverable — must still refuse rather than send nowhere."""
    pid = people_store.upsert_person(full_name="No Discord")
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": str(pid), "text": "hi"})
    assert "error" in result
    assert "roster" in result["error"].lower()


def test_send_discord_dm_archived_person_id_not_recovered() -> None:
    """A stale person_id for an ARCHIVED person must NOT be recovered — you
    can't reach an offboarded person by passing their old id."""
    pid = people_store.upsert_person(
        full_name="Gone", discord_user_id="123456789012345678"
    )
    people_store.archive_person(pid)
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": str(pid), "text": "hi"})
    assert "error" in result
    assert "roster" in result["error"].lower()


def test_send_discord_dm_malformed_stored_id_not_sent() -> None:
    """If the resolved person's stored discord_user_id is malformed (not a
    numeric snowflake), recovery must refuse rather than hand garbage to the
    Discord API."""
    pid = people_store.upsert_person(full_name="Weird", discord_user_id="@not-a-snowflake")
    fake_send = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings") as mock_get,
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
    ):
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": str(pid), "text": "hi"})
    assert "error" in result
    fake_send.assert_not_awaited()


def test_send_discord_dm_unicode_digit_id_refused_cleanly() -> None:
    """A non-ASCII 'digit' string passes str.isdigit() but breaks int() — the
    handler must return a clean JSON refusal, not raise."""
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": "１２３", "text": "hi"})
    assert "error" in result


def test_send_discord_dm_roster_error_steers_to_channel_id() -> None:
    """When refused, the error must tell the Executive to use the
    discord_user_id (snowflake) rather than the person_id, so it can
    self-correct on the next iteration instead of re-sending the person_id."""
    people_store.upsert_person(full_name="Alex", discord_user_id="123456789012345678")
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        # Pass the (small-integer) person_id by mistake, as the model did.
        result = _call(handle_send_discord_dm, {"discord_user_id": "137", "text": "hi"})
    assert "error" in result
    assert "person_id" in result["error"]
    assert "discord_user_id" in result["error"]


def test_lookup_person_zero_matches_returns_hint() -> None:
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")

    result = _call(handle_lookup_person, {"query": "nobody"})

    assert result["matches"] == []
    assert "hint" in result and "/people" in result["hint"]


def test_lookup_person_empty_query_returns_hint() -> None:
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")

    result = _call(handle_lookup_person, {"query": "   "})

    assert result["matches"] == []
    assert "hint" in result


def test_lookup_person_case_insensitive() -> None:
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")

    upper = _call(handle_lookup_person, {"query": "SARAH"})
    lower = _call(handle_lookup_person, {"query": "sarah"})

    assert len(upper["matches"]) == 1
    assert len(lower["matches"]) == 1
    assert upper["matches"][0]["person_id"] == lower["matches"][0]["person_id"]


def test_lookup_person_caps_at_5_matches() -> None:
    for i in range(7):
        people_store.upsert_person(full_name=f"Person {i}", role="Engineer")

    result = _call(handle_lookup_person, {"query": "person"})

    assert len(result["matches"]) == 5


def test_lookup_person_bad_args_returns_error() -> None:
    result = _call(handle_lookup_person, {})
    assert "error" in result


def test_lookup_person_audits_zero_match() -> None:
    """Zero-match invocations must still emit an audit row — useful for
    spotting reconnaissance / unexpected lookup volume."""
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")
    with patch("openexecutive.audit.log_event") as mock_audit:
        _call(handle_lookup_person, {"query": "nobody-matches"})
    assert mock_audit.called
    details = mock_audit.call_args.kwargs["details"]
    assert details["matches"] == 0
    assert details["ok"] is True


def test_lookup_person_caps_query_length() -> None:
    """An oversized query must be truncated before scan + audit — protects
    the audit log and bounds the substring scan."""
    people_store.upsert_person(full_name="Sarah Chen", role="CFO")
    huge_query = "z" * 5000
    with patch("openexecutive.audit.log_event") as mock_audit:
        _call(handle_lookup_person, {"query": huge_query})
    details = mock_audit.call_args.kwargs["details"]
    # 200 = _LOOKUP_PERSON_MAX_QUERY_CHARS
    assert len(details["query"]) <= 200


def test_send_discord_dm_roster_rejects_unknown_user() -> None:
    """A discord_user_id not matching any Person row must be refused.
    The People roster is now the only gate (no env allowlist)."""
    # No People rows seeded → find_person_by_discord_id returns None.
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": "999", "text": "hi"})
    assert "error" in result
    assert "roster" in result["error"].lower()


def test_send_discord_dm_roster_allows_known_user() -> None:
    """A discord_user_id matching a Person row succeeds."""
    people_store.upsert_person(full_name="Alex", discord_user_id="555")
    fake_send = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings") as mock_get,
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
        patch("openexecutive.audit.log_event"),
    ):
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": "555", "text": "hi"})
    assert result["status"] == "sent"


# --------------------------------------------------------------------------- #
# send_discord_dm
# --------------------------------------------------------------------------- #


def test_send_discord_dm_requires_token() -> None:
    """When no bot token is configured, return a structured error — not a crash."""
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = ""
        result = _call(handle_send_discord_dm, {"discord_user_id": "1", "text": "hi"})

    assert "error" in result
    assert "discord is not configured" in result["error"]


def test_send_discord_dm_rejects_empty_args() -> None:
    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": "", "text": "hi"})
    assert "error" in result

    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(handle_send_discord_dm, {"discord_user_id": "1", "text": "   "})
    assert "error" in result


def test_send_discord_dm_happy_path_audits_and_returns_sent() -> None:
    """On success the handler returns {status:sent, discord_user_id:...} and
    leaves an audit log row."""
    people_store.upsert_person(full_name="Alex", discord_user_id="123456789012345678")
    fake_send = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings") as mock_get,
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
        patch("openexecutive.audit.log_event") as mock_audit,
    ):
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(
            handle_send_discord_dm,
            {"discord_user_id": "123456789012345678", "text": "standup at 10"},
        )

    # The payload also names the channel, address and provider message id, so
    # a caller that must correlate a later inbound reply (the wait-for-human
    # gate) can do so without re-deriving them.
    assert result == {
        "status": "sent",
        "discord_user_id": "123456789012345678",
        "channel": "discord",
        "channel_ref": "123456789012345678",
        "message_id": "",
    }
    fake_send.assert_awaited_once_with("123456789012345678", "standup at 10")
    # Audit log records the outbound send with ok=True.
    assert mock_audit.called
    details = mock_audit.call_args.kwargs["details"]
    assert details["ok"] is True
    assert details["user_id"] == "123456789012345678"


def test_send_discord_dm_send_failure_returns_error_and_audits_failure() -> None:
    """When send_dm raises, the handler must catch, audit the failure, and
    return a structured error JSON — never crash the Executive turn."""
    people_store.upsert_person(full_name="Alex", discord_user_id="123")
    fake_send = AsyncMock(side_effect=RuntimeError("discord API 502"))
    with (
        patch("openexecutive.config.get_settings") as mock_get,
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
        patch("openexecutive.audit.log_event") as mock_audit,
    ):
        mock_get.return_value.discord_bot_token = "fake-token"
        result = _call(
            handle_send_discord_dm,
            {"discord_user_id": "123", "text": "hi"},
        )

    assert "error" in result
    assert "discord API 502" in result["error"]
    # Audit row is emitted with ok=False so failures are visible in the log.
    details = mock_audit.call_args.kwargs["details"]
    assert details["ok"] is False


# --------------------------------------------------------------------------- #
# discord_bot.send_dm helper
# --------------------------------------------------------------------------- #


def test_send_dm_opens_channel_then_posts_chunked_message() -> None:
    """send_dm must (1) open the DM channel via /users/@me/channels and
    (2) POST each chunk to /channels/{id}/messages. Verifies the order and
    the chunking path for messages longer than the 2000-char Discord limit."""
    from openexecutive.integrations import discord_bot

    long_text = "x" * 2500  # exceeds the 2000-char Discord limit

    # Two mock responses: first for open-channel, then one per posted chunk.
    # httpx Response.raise_for_status and .json are sync, so use plain MagicMock.
    from unittest.mock import MagicMock
    open_response = MagicMock()
    open_response.raise_for_status = MagicMock()
    open_response.json = MagicMock(return_value={"id": "9999"})

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    posted_urls: list[str] = []

    async def fake_post(url, **kwargs):
        posted_urls.append(url)
        if "/users/@me/channels" in url:
            return open_response
        return post_response

    fake_client = AsyncMock()
    fake_client.post = fake_post
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None

    with (
        patch("openexecutive.config.get_settings") as mock_get,
        patch("httpx.AsyncClient", return_value=fake_client),
    ):
        mock_get.return_value.discord_bot_token = "fake-token"
        asyncio.run(discord_bot.send_dm("123456789012345678", long_text))

    # First call must open the channel; subsequent calls post to that channel.
    assert posted_urls[0].endswith("/users/@me/channels")
    assert all(url.endswith("/channels/9999/messages") for url in posted_urls[1:])
    # 2500 chars > 2000 limit, so chunking must produce at least 2 sends.
    assert len(posted_urls) >= 3  # 1 open + 2+ posts


def test_send_dm_raises_when_token_missing() -> None:
    """send_dm must raise (not silently no-op) so the calling tool handler
    can return a structured error to the Executive."""
    from openexecutive.integrations import discord_bot

    with patch("openexecutive.config.get_settings") as mock_get:
        mock_get.return_value.discord_bot_token = ""
        with pytest.raises(RuntimeError, match="token is not configured"):
            asyncio.run(discord_bot.send_dm("123", "hi"))


# --------------------------------------------------------------------------- #
# Registration check
# --------------------------------------------------------------------------- #


def test_new_tools_registered_in_schedule_tools() -> None:
    """Both tool dicts must appear in SCHEDULE_TOOLS and their handlers in
    SCHEDULE_TOOL_HANDLERS — otherwise the Executive will never see them."""
    from openexecutive.orchestrator.schedule_tools import (
        SCHEDULE_TOOL_HANDLERS,
        SCHEDULE_TOOLS,
    )

    tool_names = {t["name"] for t in SCHEDULE_TOOLS}
    assert "lookup_person" in tool_names
    assert "send_discord_dm" in tool_names

    assert "lookup_person" in SCHEDULE_TOOL_HANDLERS
    assert "send_discord_dm" in SCHEDULE_TOOL_HANDLERS
