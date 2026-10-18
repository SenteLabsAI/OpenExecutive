"""The chat directory lists Slack, Discord and Telegram people and rooms by
name for the People and department pickers, with no network in these tests
(an ``httpx.MockTransport`` stands in for the providers)."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat_directory as directory_route
from openexecutive.integrations import chat_directory, telegram_chats
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

SLACK_TOKEN = "xoxb-test-token-not-real"
DISCORD_TOKEN = "discord-test-token-not-real"
TELEGRAM_TOKEN = "123456789:AAH" + "x" * 32

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SLACK_BOT_TOKEN", "DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "DISCORD_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(telegram_chats, "DB_PATH", tmp_path / "episodic.db")
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "episodic.db")
    people_registry.invalidate()
    chat_directory.clear_cache()
    yield
    chat_directory.clear_cache()


def _transport(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        chat_directory,
        "client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
    )
    return seen


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #

SLACK_USERS_PAGE_1 = {
    "ok": True,
    "members": [
        {
            "id": "U001",
            "name": "maya",
            "profile": {"real_name": "Maya Lindqvist", "display_name": "maya", "email": "Maya@Example.com"},
        },
        {"id": "U002", "name": "helperbot", "is_bot": True, "profile": {"real_name": "Helper"}},
        {"id": "USLACKBOT", "name": "slackbot", "profile": {"real_name": "Slackbot"}},
    ],
    "response_metadata": {"next_cursor": "page2"},
}
SLACK_USERS_PAGE_2 = {
    "ok": True,
    "members": [
        {"id": "U003", "name": "gone", "deleted": True, "profile": {"real_name": "Gone"}},
        {"id": "U004", "name": "dev", "profile": {"real_name": "Dev Okafor"}},
    ],
    "response_metadata": {"next_cursor": ""},
}


def _slack_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "slack.com"
    assert request.headers["Authorization"] == f"Bearer {SLACK_TOKEN}"
    method = request.url.path.rsplit("/", 1)[-1]
    if method == "users.list":
        page = SLACK_USERS_PAGE_2 if request.url.params.get("cursor") == "page2" else SLACK_USERS_PAGE_1
        return httpx.Response(200, json=page)
    if method == "conversations.list":
        return httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [
                    {"id": "C02", "name": "sales", "is_private": False},
                    {"id": "C01", "name": "finance-team", "is_private": True},
                ],
            },
        )
    if method == "users.lookupByEmail":
        if request.url.params.get("email") == "dev@example.com":
            return httpx.Response(200, json={"ok": True, "user": SLACK_USERS_PAGE_2["members"][1]})  # type: ignore[index]
        return httpx.Response(200, json={"ok": False, "error": "users_not_found"})
    return httpx.Response(404)


async def test_slack_people_skip_bots_and_deactivated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    seen = _transport(monkeypatch, _slack_handler)

    listing = await chat_directory.list_directory("people")

    slack = listing.slack
    assert slack.configured and slack.ok and not slack.truncated
    assert [(o.id, o.name, o.detail, o.email) for o in slack.options] == [
        ("U004", "Dev Okafor", "@dev", None),
        ("U001", "Maya Lindqvist", "@maya", "maya@example.com"),
    ]
    assert len([r for r in seen if r.url.path.endswith("users.list")]) == 2
    # The other providers have no token: empty, not configured.
    assert not listing.discord.configured and listing.discord.options == []
    assert not listing.telegram.configured


async def test_slack_channels_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    seen = _transport(monkeypatch, _slack_handler)

    first = await chat_directory.list_directory("channels")
    second = await chat_directory.list_directory("channels")

    assert [(o.id, o.name, o.detail) for o in first.slack.options] == [
        ("C01", "#finance-team", "Private"),
        ("C02", "#sales", ""),
    ]
    assert second == first
    assert len(seen) == 1  # the second read came from the cache


async def test_slack_channels_fall_back_to_public_without_groups_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        if "private_channel" in (request.url.params.get("types") or ""):
            return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
        return httpx.Response(200, json={"ok": True, "channels": [{"id": "C02", "name": "sales"}]})

    _transport(monkeypatch, handler)
    listing = await chat_directory.list_directory("channels")
    assert [o.id for o in listing.slack.options] == ["C02"]


async def test_slack_failure_is_an_empty_list_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))

    listing = await chat_directory.list_directory("people")
    assert listing.slack.configured and not listing.slack.ok and listing.slack.options == []
    assert SLACK_TOKEN not in listing.model_dump_json()


async def test_slack_lookup_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    _transport(monkeypatch, _slack_handler)

    found = await chat_directory.slack_user_by_email(" Dev@Example.com ")
    assert found.configured and found.match is not None
    assert (found.match.id, found.match.name) == ("U004", "Dev Okafor")

    missing = await chat_directory.slack_user_by_email("nobody@example.com")
    assert missing.configured and missing.match is None


async def test_slack_lookup_uses_the_cached_list_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    seen = _transport(monkeypatch, _slack_handler)
    await chat_directory.list_directory("people")
    calls = len(seen)

    found = await chat_directory.slack_user_by_email("maya@example.com")
    assert found.match is not None and found.match.id == "U001"
    assert len(seen) == calls


async def test_slack_lookup_without_a_token() -> None:
    assert (await chat_directory.slack_user_by_email("a@example.com")).configured is False


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

def _discord_handler(members_status: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "discord.com"
        assert request.headers["Authorization"] == f"Bot {DISCORD_TOKEN}"
        path = request.url.path.removeprefix("/api/v10")
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "900", "name": "Example Co"}, {"id": "901", "name": "Other"}])
        if path.endswith("/channels"):
            return httpx.Response(
                200,
                json=[
                    {"id": "11", "type": 0, "name": "general", "position": 1},
                    {"id": "12", "type": 2, "name": "voice", "position": 2},
                    {"id": "13", "type": 5, "name": "news", "position": 0},
                    {"id": "14", "type": 4, "name": "category", "position": 3},
                ],
            )
        if path.endswith("/members"):
            if members_status != 200:
                return httpx.Response(members_status, json={"message": "Missing Access", "code": 50001})
            return httpx.Response(
                200,
                json=[
                    {"user": {"id": "21", "username": "priya", "global_name": "Priya Raman"}},
                    {"nick": "Sam", "user": {"id": "22", "username": "sam.k"}},
                    {"user": {"id": "23", "username": "ourbot", "bot": True}},
                ],
            )
        return httpx.Response(404)

    return handler


async def test_discord_channels_of_configured_guilds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", DISCORD_TOKEN)
    monkeypatch.setenv("DISCORD_GUILD_IDS", "900")
    seen = _transport(monkeypatch, _discord_handler())

    listing = await chat_directory.list_directory("channels")

    assert [(o.id, o.name, o.detail) for o in listing.discord.options] == [
        ("13", "#news", "Example Co"),
        ("11", "#general", "Example Co"),
    ]
    assert not any("/guilds/901/" in str(r.url) for r in seen)


async def test_discord_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", DISCORD_TOKEN)
    monkeypatch.setenv("DISCORD_GUILD_IDS", "900")
    _transport(monkeypatch, _discord_handler())

    listing = await chat_directory.list_directory("people")

    assert listing.discord.members_available is True
    assert [(o.id, o.name, o.detail) for o in listing.discord.options] == [
        ("21", "Priya Raman", "@priya"),
        ("22", "Sam", "@sam.k"),
    ]


async def test_discord_members_refused_is_flagged_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", DISCORD_TOKEN)
    _transport(monkeypatch, _discord_handler(members_status=403))

    listing = await chat_directory.list_directory("people")

    assert listing.discord.configured and listing.discord.ok
    assert listing.discord.members_available is False
    assert listing.discord.options == []


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def _update(chat: dict[str, Any], **message: Any) -> dict[str, Any]:
    return {"update_id": 1, "message": {"message_id": 5, "chat": chat, **message}}


def test_telegram_records_private_chats_and_groups() -> None:
    telegram_chats.record_from_update(
        _update({"id": 42, "type": "private", "first_name": "Noor", "last_name": "Haddad", "username": "noorh"})
    )
    telegram_chats.record_from_update(_update({"id": -100777, "type": "supergroup", "title": "Ops room"}))
    telegram_chats.record_from_update(_update({"id": -5, "type": "channel", "title": "Broadcast"}))

    people = telegram_chats.list_chats("people")
    groups = telegram_chats.list_chats("groups")
    assert [(c.chat_id, c.title, c.username) for c in people] == [("42", "Noor Haddad", "noorh")]
    assert [(c.chat_id, c.title) for c in groups] == [("-100777", "Ops room")]


def test_telegram_upsert_keeps_one_row_and_updates_last_seen() -> None:
    chat = {"id": 42, "type": "private", "first_name": "Noor"}
    telegram_chats.record_from_update(_update(chat))
    first = telegram_chats.list_chats("people")[0]
    telegram_chats.record_from_update(_update({**chat, "first_name": "Noor", "last_name": "H"}))
    rows = telegram_chats.list_chats("people")
    assert len(rows) == 1
    assert rows[0].title == "Noor H"
    assert rows[0].first_seen_at == first.first_seen_at


def test_telegram_forgets_a_migrated_group_and_one_the_bot_left() -> None:
    telegram_chats.record_from_update(_update({"id": -10, "type": "group", "title": "Old"}))
    telegram_chats.record_from_update(
        _update({"id": -10, "type": "group", "title": "Old"}, migrate_to_chat_id=-100999)
    )
    assert telegram_chats.list_chats("groups") == []

    telegram_chats.record_from_update(
        {"my_chat_member": {"chat": {"id": -20, "type": "group", "title": "Team"},
                            "new_chat_member": {"status": "member"}}}
    )
    assert [c.chat_id for c in telegram_chats.list_chats("groups")] == ["-20"]
    telegram_chats.record_from_update(
        {"my_chat_member": {"chat": {"id": -20, "type": "group", "title": "Team"},
                            "new_chat_member": {"status": "kicked"}}}
    )
    assert telegram_chats.list_chats("groups") == []


def test_telegram_reads_never_create_the_db(tmp_path: Path) -> None:
    missing = tmp_path / "nothing-here.db"
    assert telegram_chats.list_chats("people", db_path=missing) == []
    assert not missing.exists()


def test_telegram_bad_update_is_ignored() -> None:
    telegram_chats.record_from_update({"message": {"chat": {"id": "x", "type": "private"}}})
    telegram_chats.record_from_update({"message": "nope"})
    assert telegram_chats.list_chats("people") == []


async def test_telegram_directory_from_seen_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
    telegram_chats.record_from_update(_update({"id": 42, "type": "private", "first_name": "Noor", "username": "noorh"}))
    telegram_chats.record_from_update(_update({"id": 43, "type": "private", "username": "anon"}))
    telegram_chats.record_from_update(_update({"id": -100777, "type": "supergroup", "title": "Ops room"}))

    people = (await chat_directory.list_directory("people")).telegram
    channels = (await chat_directory.list_directory("channels")).telegram
    assert people.configured
    details = {o.id: (o.name, o.detail) for o in people.options}
    assert details["42"][0] == "Noor"
    assert details["42"][1].startswith("@noorh · first wrote ")
    assert details["43"][0] == "@anon"
    assert details["43"][1].startswith("first wrote ")
    assert [(o.id, o.name) for o in channels.options] == [("-100777", "Ops room")]


def _telegram_app() -> TestClient:
    from openexecutive.integrations.telegram_bot import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_webhook_records_the_chat_only_when_its_secret_checked_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openexecutive.audit as audit

    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
    people_store.initialize_db()
    # Not on the roster and a group: the update is otherwise ignored.
    update = _update({"id": -100777, "type": "supergroup", "title": "Ops room"}, text="hi")

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert _telegram_app().post("/webhook/telegram", json=update).status_code == 200
    assert telegram_chats.list_chats("groups") == []

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    resp = _telegram_app().post(
        "/webhook/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}
    )
    assert resp.status_code == 200
    assert [c.title for c in telegram_chats.list_chats("groups")] == ["Ops room"]


# --------------------------------------------------------------------------- #
# Routes: the principal's alone
# --------------------------------------------------------------------------- #

OWNER = {"x-caller-email": "olive@example.com"}
TEAMMATE = {"x-caller-email": "tom@example.com"}
STRANGER = {"x-caller-email": "sam@example.com"}


@pytest.fixture()
def client() -> TestClient:
    people_store.initialize_db()
    app = FastAPI()
    app.include_router(directory_route.router)
    return TestClient(app)


def _seed_team() -> None:
    for name, email, principal in (("Olive", "olive@example.com", True), ("Tom", "tom@example.com", False)):
        people_store.upsert_person(full_name=name, email=email, is_principal=principal)
    people_registry.invalidate()


def test_routes_answer_the_owner(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_team()
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    _transport(monkeypatch, _slack_handler)

    people = client.get("/chat-directory/people", headers=OWNER)
    channels = client.get("/chat-directory/channels", headers=OWNER)
    lookup = client.get("/chat-directory/slack/lookup", params={"email": "dev@example.com"}, headers=OWNER)

    assert (people.status_code, channels.status_code, lookup.status_code) == (200, 200, 200)
    body = people.json()
    assert set(body) == {"slack", "discord", "telegram"}
    assert body["discord"] == {
        "configured": False, "ok": True, "options": [], "truncated": False, "members_available": None,
    }
    assert lookup.json()["match"]["id"] == "U004"
    assert SLACK_TOKEN not in json.dumps([people.json(), channels.json(), lookup.json()])


@pytest.mark.parametrize("caller", [TEAMMATE, STRANGER], ids=["teammate", "unrostered"])
def test_routes_refuse_everyone_else(client: TestClient, caller: dict[str, str]) -> None:
    _seed_team()
    for path in ("/chat-directory/people", "/chat-directory/channels"):
        assert client.get(path, headers=caller).status_code == 403
    lookup = client.get("/chat-directory/slack/lookup", params={"email": "a@example.com"}, headers=caller)
    assert lookup.status_code == 403


def test_first_setup_opens_channels_but_not_people(client: TestClient) -> None:
    # No principal yet: whoever is setting up may pick channels, but the
    # people lists (names and emails) wait until someone is principal.
    assert client.get("/chat-directory/channels", headers=STRANGER).status_code == 200
    assert client.get("/chat-directory/people", headers=STRANGER).status_code == 403
    lookup = client.get("/chat-directory/slack/lookup", params={"email": "a@example.com"}, headers=STRANGER)
    assert lookup.status_code == 403


def test_telegram_groups_never_push_people_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_chats, "MAX_ROWS", 3)
    telegram_chats.record_from_update(_update({"id": 42, "type": "private", "first_name": "Noor"}))
    for i in range(10):
        telegram_chats.record_from_update(_update({"id": -100 - i, "type": "group", "title": f"Room {i}"}))
    assert [c.chat_id for c in telegram_chats.list_chats("people")] == ["42"]
    assert len(telegram_chats.list_chats("groups")) == 3


async def test_telegram_option_says_who_already_has_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
    people_store.initialize_db()
    people_store.upsert_person(full_name="Olive", email="olive@example.com", telegram_chat_id="42")
    people_registry.invalidate()
    # Someone else names themselves after Olive.
    telegram_chats.record_from_update(_update({"id": 42, "type": "private", "first_name": "Olive"}))
    telegram_chats.record_from_update(_update({"id": 77, "type": "private", "first_name": "Olive"}))
    options = {o.id: o.detail for o in (await chat_directory.list_directory("people")).telegram.options}
    assert options["42"].endswith("already linked to Olive")
    assert "already linked" not in options["77"]
