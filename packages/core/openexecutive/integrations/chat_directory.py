"""Who and where the chat bots can reach, by name, for the People and
department pickers (``GET /chat-directory/*``, ``api/routes/chat_directory.py``).

So nobody has to copy a raw Slack, Discord or Telegram id by hand, this lists
from the bots this instance runs:

- **Slack** (``SLACK_BOT_TOKEN``): channels from ``conversations.list``
  (public ones and the private ones the bot is in; scopes ``channels:read``,
  ``groups:read``) and people from ``users.list`` (no bots, no deactivated
  accounts; ``users:read``, plus ``users:read.email`` for their profile
  email). ``slack_user_by_email`` finds one person by work email
  (``users.lookupByEmail``).
- **Discord** (``DISCORD_BOT_TOKEN``): text and announcement channels of the
  servers the bot is in (only ``DISCORD_GUILD_IDS`` when set), and their
  members. Listing members needs the Server Members privileged intent; when
  Discord refuses, the members list is empty with ``members_available`` false
  rather than an error.
- **Telegram** (``TELEGRAM_BOT_TOKEN``): Telegram cannot list chats, so this
  reads the ones the bot has seen (``integrations.telegram_chats``): private
  chats are people, groups are rooms.

Each list is cached in-process for ``CACHE_TTL_S`` seconds and capped at
``MAX_ITEMS``. A provider with no token reads as ``configured=False`` with no
options; a provider that fails reads as ``ok=False`` with no options. Tokens
never leave this module: they go only in the Authorization header to the
provider's own API host.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from openexecutive.config import get_settings

logger = logging.getLogger(__name__)

CACHE_TTL_S = 60.0
MAX_ITEMS = 2000
_PAGE = 200
_MAX_PAGES = MAX_ITEMS // _PAGE
_TIMEOUT_S = 10.0

SLACK_API = "https://slack.com/api/"
DISCORD_API = "https://discord.com/api/v10"

# Discord channel types the Executive can post to: text and announcement.
_DISCORD_TEXT_TYPES = frozenset({0, 5})

Provider = Literal["slack", "discord", "telegram"]
Kind = Literal["people", "channels"]


class DirectoryOption(BaseModel):
    """One pickable person or room. ``id`` is what gets saved; ``name`` and
    ``detail`` are what the picker shows."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    detail: str = ""
    email: str | None = None


class ProviderList(BaseModel):
    configured: bool
    ok: bool = True
    options: list[DirectoryOption] = Field(default_factory=list)
    truncated: bool = False
    # Discord people only: False when Discord refused to list members (the
    # Server Members intent is off). None elsewhere.
    members_available: bool | None = None


class DirectoryListing(BaseModel):
    slack: ProviderList
    discord: ProviderList
    telegram: ProviderList


class SlackMatch(BaseModel):
    configured: bool
    match: DirectoryOption | None = None


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

_cache: dict[tuple[str, str], tuple[float, ProviderList]] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Tests swap this for a client on an ``httpx.MockTransport``.
ClientFactory = Callable[[], httpx.AsyncClient]


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT_S)


client_factory: ClientFactory = _default_client


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


async def _cached(
    key: tuple[str, str], load: Callable[[], Awaitable[ProviderList]]
) -> ProviderList:
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
        result = await load()
        _cache[key] = (time.monotonic(), result)
        return result


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #

class _SlackError(Exception):
    pass


async def _slack_call(
    client: httpx.AsyncClient, token: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    resp = await client.get(
        SLACK_API + method, params=params, headers={"Authorization": f"Bearer {token}"}
    )
    if resp.status_code == 429:
        raise _SlackError("rate_limited")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise _SlackError("bad_response")
    if not data.get("ok"):
        raise _SlackError(str(data.get("error") or "not_ok"))
    return data


async def _slack_paged(
    token: str, method: str, params: dict[str, Any], items_key: str
) -> tuple[list[dict[str, Any]], bool]:
    """All pages of a cursor-paginated Slack list, up to ``_MAX_PAGES``.
    Returns (items, truncated). A failure after the first page keeps what
    was read and marks it truncated."""
    items: list[dict[str, Any]] = []
    cursor = ""
    async with client_factory() as client:
        for page in range(_MAX_PAGES):
            query = {**params, "limit": _PAGE}
            if cursor:
                query["cursor"] = cursor
            try:
                data = await _slack_call(client, token, method, query)
            except (_SlackError, httpx.HTTPError):
                if page == 0:
                    raise
                return items, True
            items.extend(x for x in data.get(items_key) or [] if isinstance(x, dict))
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return items, False
    return items, True


def _slack_person(user: dict[str, Any]) -> DirectoryOption | None:
    uid = user.get("id")
    if (
        not isinstance(uid, str)
        or not uid
        or uid == "USLACKBOT"
        or user.get("deleted")
        or user.get("is_bot")
        or user.get("is_app_user")
    ):
        return None
    profile = user.get("profile") or {}
    handle = str(profile.get("display_name") or user.get("name") or "").strip()
    name = str(profile.get("real_name") or user.get("real_name") or handle or uid).strip()
    email = profile.get("email")
    return DirectoryOption(
        id=uid,
        name=name,
        detail=f"@{handle}" if handle else "",
        email=email.strip().lower() if isinstance(email, str) and email.strip() else None,
    )


async def _load_slack_people(token: str) -> ProviderList:
    users, truncated = await _slack_paged(token, "users.list", {}, "members")
    options = [o for o in (_slack_person(u) for u in users) if o is not None]
    options.sort(key=lambda o: o.name.lower())
    return ProviderList(configured=True, options=options[:MAX_ITEMS], truncated=truncated)


async def _load_slack_channels(token: str) -> ProviderList:
    params = {"types": "public_channel,private_channel", "exclude_archived": "true"}
    try:
        channels, truncated = await _slack_paged(token, "conversations.list", params, "channels")
    except _SlackError as exc:
        # Without groups:read Slack refuses the whole call: list the public
        # channels rather than nothing.
        if str(exc) != "missing_scope":
            raise
        params["types"] = "public_channel"
        channels, truncated = await _slack_paged(token, "conversations.list", params, "channels")
    options: list[DirectoryOption] = []
    for ch in channels:
        cid, name = ch.get("id"), ch.get("name")
        if not isinstance(cid, str) or not isinstance(name, str):
            continue
        options.append(
            DirectoryOption(id=cid, name=f"#{name}", detail="Private" if ch.get("is_private") else "")
        )
    options.sort(key=lambda o: o.name.lower())
    return ProviderList(configured=True, options=options[:MAX_ITEMS], truncated=truncated)


async def slack_user_by_email(email: str) -> SlackMatch:
    """The Slack person whose profile email is ``email``, or no match."""
    token = get_settings().slack_bot_token
    if not token:
        return SlackMatch(configured=False)
    wanted = email.strip().lower()
    if not wanted or "@" not in wanted:
        return SlackMatch(configured=True)
    # The cached list usually has them already.
    hit = _cache.get(("slack", "people"))
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
        for opt in hit[1].options:
            if opt.email == wanted:
                return SlackMatch(configured=True, match=opt)
    try:
        async with client_factory() as client:
            data = await _slack_call(client, token, "users.lookupByEmail", {"email": wanted})
    except _SlackError as exc:
        if str(exc) != "users_not_found":
            logger.warning("chat directory: Slack lookup by email failed: %s", exc)
        return SlackMatch(configured=True)
    except httpx.HTTPError as exc:
        logger.warning("chat directory: Slack lookup by email failed: %s", type(exc).__name__)
        return SlackMatch(configured=True)
    user = data.get("user")
    match = _slack_person(user) if isinstance(user, dict) else None
    return SlackMatch(configured=True, match=match)


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

class _DiscordRefused(Exception):
    """Discord answered 401/403 (for members: the intent is off)."""


async def _discord_get(client: httpx.AsyncClient, token: str, path: str, **params: Any) -> Any:
    resp = await client.get(
        DISCORD_API + path, params=params or None, headers={"Authorization": f"Bot {token}"}
    )
    if resp.status_code in (401, 403):
        raise _DiscordRefused(str(resp.status_code))
    resp.raise_for_status()
    return resp.json()


async def _discord_guilds(client: httpx.AsyncClient, token: str) -> list[tuple[str, str]]:
    """(id, name) of the servers to list: ``DISCORD_GUILD_IDS`` when set
    (and the bot is in them), else every server the bot is in."""
    data = await _discord_get(client, token, "/users/@me/guilds", limit=200)
    guilds = [
        (str(g["id"]), str(g.get("name") or ""))
        for g in data or []
        # Ids go into request paths: only Discord's numeric snowflakes.
        if isinstance(g, dict) and str(g.get("id") or "").isdigit()
    ]
    only = {str(g) for g in get_settings().discord_guild_ids}
    return [g for g in guilds if g[0] in only] if only else guilds


async def _load_discord_channels(token: str) -> ProviderList:
    options: list[DirectoryOption] = []
    async with client_factory() as client:
        guilds = await _discord_guilds(client, token)
        for gid, gname in guilds:
            channels = await _discord_get(client, token, f"/guilds/{gid}/channels")
            rows = sorted(
                (c for c in channels or [] if isinstance(c, dict) and c.get("type") in _DISCORD_TEXT_TYPES),
                key=lambda c: (c.get("position") or 0, str(c.get("name") or "")),
            )
            options.extend(
                DirectoryOption(id=str(c["id"]), name=f"#{c.get('name') or ''}", detail=gname)
                for c in rows
                if c.get("id") is not None
            )
    truncated = len(options) > MAX_ITEMS
    return ProviderList(configured=True, options=options[:MAX_ITEMS], truncated=truncated)


async def _load_discord_people(token: str) -> ProviderList:
    seen: dict[str, DirectoryOption] = {}
    truncated = False
    async with client_factory() as client:
        guilds = await _discord_guilds(client, token)
        for gid, _gname in guilds:
            after = "0"
            for _ in range(_MAX_PAGES):
                try:
                    page = await _discord_get(
                        client, token, f"/guilds/{gid}/members", limit=1000, after=after
                    )
                except _DiscordRefused:
                    return ProviderList(configured=True, members_available=False)
                members = [m for m in page or [] if isinstance(m, dict)]
                for m in members:
                    user = m.get("user") or {}
                    uid = user.get("id")
                    if uid is None or user.get("bot") or str(uid) in seen:
                        continue
                    username = str(user.get("username") or "")
                    name = str(m.get("nick") or user.get("global_name") or username or uid)
                    seen[str(uid)] = DirectoryOption(
                        id=str(uid), name=name, detail=f"@{username}" if username else ""
                    )
                if len(members) < 1000:
                    break
                after = str((members[-1].get("user") or {}).get("id") or "")
                if not after:
                    break
            else:
                truncated = True
            if len(seen) >= MAX_ITEMS:
                truncated = True
                break
    options = sorted(seen.values(), key=lambda o: o.name.lower())
    return ProviderList(
        configured=True, options=options[:MAX_ITEMS], truncated=truncated, members_available=True
    )


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def _telegram_list(kind: Kind) -> ProviderList:
    from openexecutive.integrations.telegram_chats import list_chats

    chats = list_chats("people" if kind == "people" else "groups")
    options = [
        DirectoryOption(
            id=c.chat_id,
            name=c.title or (f"@{c.username}" if c.username else "Telegram chat"),
            detail=f"@{c.username}" if c.username and c.title else "",
        )
        for c in chats
    ]
    return ProviderList(configured=True, options=options)


# --------------------------------------------------------------------------- #
# Public
# --------------------------------------------------------------------------- #

_LOADERS: dict[tuple[str, str], Callable[[str], Awaitable[ProviderList]]] = {
    ("slack", "people"): _load_slack_people,
    ("slack", "channels"): _load_slack_channels,
    ("discord", "people"): _load_discord_people,
    ("discord", "channels"): _load_discord_channels,
}


async def _provider_list(provider: Provider, kind: Kind) -> ProviderList:
    settings = get_settings()
    token = {
        "slack": settings.slack_bot_token,
        "discord": settings.discord_bot_token,
        "telegram": settings.telegram_bot_token,
    }[provider]
    if not token:
        return ProviderList(configured=False)
    if provider == "telegram":
        # A local read, fresh each time: a chat that just wrote shows at once.
        try:
            return _telegram_list(kind)
        except Exception:
            logger.exception("chat directory: reading Telegram chats failed")
            return ProviderList(configured=True, ok=False)
    loader = _LOADERS[(provider, kind)]

    async def load() -> ProviderList:
        try:
            return await loader(token)
        except Exception as exc:
            # The exception text can carry a request URL, never a token
            # (tokens go only in headers), but keep the log to its kind.
            logger.warning(
                "chat directory: listing %s %s failed: %s %s",
                provider, kind, type(exc).__name__, exc if isinstance(exc, _SlackError) else "",
            )
            return ProviderList(configured=True, ok=False)

    return await _cached((provider, kind), load)


async def list_directory(kind: Kind) -> DirectoryListing:
    """Every provider's people or channels, fetched side by side."""
    slack, discord, telegram = await asyncio.gather(
        _provider_list("slack", kind),
        _provider_list("discord", kind),
        _provider_list("telegram", kind),
    )
    return DirectoryListing(slack=slack, discord=discord, telegram=telegram)
