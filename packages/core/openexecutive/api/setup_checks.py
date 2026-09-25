"""Setup status: one plain-language light per part of an install.

Backs ``GET /setup/status`` (api/routes/setup_status.py), which the UI's
Settings → Setup status page renders next to the sign-in light it works out
itself. Each check answers "is this part working — and if not, what exactly
do I do?" for someone who has never read this code.

Every check keeps three rules:
- No secret in a response or a log line. A probe that carries a token reports
  a status code, a service's short error code or an exception's class name —
  never the exception's text, which can quote the request URL.
- At most one cheap, read-only call per service. Nothing here sends a
  message, spends model tokens or changes state.
- Every call is bounded by ``PROBE_TIMEOUT_S``: a slow service turns amber
  instead of stalling the page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import anthropic
import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from openexecutive.audit import AuditEvent
    from openexecutive.config import Settings
    from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

State = Literal["ok", "warn", "error", "off"]

PROBE_TIMEOUT_S = 5.0

# The scheduler records every tick. Three missed ticks, and never less than a
# minute and a half, means it has stopped rather than being between ticks.
_SCHEDULER_STALE_TICKS = 3
_SCHEDULER_STALE_FLOOR_S = 90
# Boot work before the first tick (seeding briefs, a rotation reconcile) takes
# seconds; a scheduler with no tick this long after starting is stuck.
_SCHEDULER_STARTUP_GRACE_S = 300
# Telegram reports its last failed delivery until the next one fails. Older
# than this, it says nothing about whether delivery works now. The same
# window applies to a message turned away because its sender wasn't on the
# team list: after a day it is history, not something to fix.
_RECENT_FAILURE_S = 24 * 60 * 60
# Longest service-supplied text (a Telegram error, a Slack workspace name)
# echoed back. The UI renders it as plain text; this only keeps it one line.
_ECHO_MAX_CHARS = 160
_SERVICE_ACCOUNT_KEY_MAX_BYTES = 64 * 1024

# Sample values .env.example ships. One still in place means the file was
# copied and that line never filled in. tests/unit/test_setup_checks.py fails
# if .env.example ships a sample this set doesn't know.
EXAMPLE_VALUES: frozenset[str] = frozenset(
    {"sk-ant-your-key-here", "xoxb-your-bot-token", "xapp-your-app-token"}
)
# RFC 2606 names reserved for examples; no real mailbox lives at them.
_EXAMPLE_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})

_TELEGRAM_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
_SLACK_ERROR_CODE_RE = re.compile(r"[a-z_]{1,40}")
# The sender id in the "Rejected: …" audit summaries the channel adapters
# write when someone off the team list messages the Executive.
_REJECTED_SENDER_RE = re.compile(r"(?:user|chat_id)=([\w.-]{1,64})")

_OFFLINE_FIX = "Check this computer's internet connection, then check again."
_RESTART = "then restart the app."


class SetupCheck(BaseModel):
    id: str
    label: str
    state: State
    summary: str
    fix: str | None = None
    # An in-app page that helps with the fix, e.g. "/people".
    link: str | None = None
    # When the channel last received a message (the audit log's ISO time).
    last_activity: str | None = None


# Every check, in display order: what the Executive needs to work at all, then
# the channels people reach it through, then what runs in the background.
LABELS: dict[str, str] = {
    "ai_model": "AI model",
    "company": "Company setup",
    "owner": "Owner",
    "exec_email": "The Executive's email address",
    "api_secret": "API protection",
    "gmail": "Email (Gmail)",
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "google_chat": "Google Chat",
    "scheduler": "Daily schedule",
    "memory": "Long-term memory (Honcho)",
}


def _result(
    check_id: str,
    state: State,
    summary: str,
    fix: str | None = None,
    *,
    link: str | None = None,
    last_activity: str | None = None,
) -> SetupCheck:
    return SetupCheck(
        id=check_id,
        label=LABELS[check_id],
        state=state,
        summary=summary,
        fix=fix,
        link=link,
        last_activity=last_activity,
    )


@dataclass(frozen=True)
class Snapshot:
    """What the checks read, gathered once per run."""

    settings: Settings
    now: datetime
    local_login: bool
    people: Sequence[Person]
    principal: Person | None
    # Latest integration_inbound audit row per channel actor.
    last_inbound: dict[str, AuditEvent | None]
    discord_bot: Any = None
    discord_bot_task: asyncio.Task[None] | None = None
    slack_handler: Any = None
    mcp_gateway: Any = None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _ECHO_MAX_CHARS else text[: _ECHO_MAX_CHARS - 1] + "…"


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute ago"
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return f"{minutes // 60} hours ago"


def is_example_email(address: str) -> bool:
    domain = address.rpartition("@")[2].strip().lower()
    return domain in _EXAMPLE_EMAIL_DOMAINS or domain.endswith(".example")


# ---------------------------------------------------------------------------
# AI model, company and the people running it
# ---------------------------------------------------------------------------


async def check_ai_model(snap: Snapshot) -> SetupCheck:
    settings = snap.settings
    key = (settings.anthropic_api_key or "").strip()
    if not key:
        # Settings refuses to load with no provider at all, so one of these is on.
        using = " and ".join(
            name
            for name, on in (
                ("OpenRouter", settings.openrouter_enabled),
                ("local models", settings.local_models_enabled),
            )
            if on
        )
        return _result(
            "ai_model", "ok", f"Using {using} instead of Anthropic. This page doesn't test that connection."
        )
    if key in EXAMPLE_VALUES:
        return _result(
            "ai_model",
            "error",
            "ANTHROPIC_API_KEY still has the sample value from .env.example, so the Executive can't answer.",
            f"Paste your key from console.anthropic.com over it in the .env file, {_RESTART}",
        )

    from openexecutive.providers.anthropic_provider import configured_async_client

    client = configured_async_client(timeout=PROBE_TIMEOUT_S).with_options(max_retries=0)
    try:
        # Listing models is free, and it goes through the same key and
        # workspace header every chat turn uses.
        await asyncio.wait_for(client.models.list(limit=1), PROBE_TIMEOUT_S)
    except anthropic.AuthenticationError:
        return _result(
            "ai_model",
            "error",
            "Anthropic turned down the key in ANTHROPIC_API_KEY.",
            f"Create a new key at console.anthropic.com, paste it into the .env file, {_RESTART}",
        )
    except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as exc:
        if not settings.anthropic_workspace_id:
            return _result(
                "ai_model",
                "error",
                f"Anthropic refused requests made with this key (HTTP {exc.status_code}).",
                "If the key was created for your whole organisation rather than inside a workspace, set "
                f"ANTHROPIC_WORKSPACE_ID in .env to the workspace to use, {_RESTART}",
            )
        return _result(
            "ai_model",
            "error",
            "Anthropic refused requests made with this key and ANTHROPIC_WORKSPACE_ID "
            f"(HTTP {exc.status_code}).",
            f"Check in console.anthropic.com that the key belongs to that workspace, {_RESTART}",
        )
    except anthropic.RateLimitError:
        return _result(
            "ai_model",
            "warn",
            "The key works, but Anthropic says it is over its rate limit right now.",
            "Wait a minute and check again. If it keeps happening, raise the limit in console.anthropic.com.",
        )
    except anthropic.APIStatusError as exc:
        return _result(
            "ai_model",
            "warn",
            f"Anthropic had a problem answering (HTTP {exc.status_code}). That is usually brief.",
            "Check again in a few minutes.",
        )
    except (anthropic.APIConnectionError, TimeoutError):
        return _result("ai_model", "warn", "Couldn't reach Anthropic to test the key.", _OFFLINE_FIX)
    finally:
        await client.close()
    return _result("ai_model", "ok", "Connected to Anthropic.")


def check_company(snap: Snapshot) -> SetupCheck:
    from openexecutive.onboarding.profile_builder import load_or_create_profile

    profile = load_or_create_profile(snap.settings.company_profile_path)
    if profile.is_empty():
        return _result(
            "company",
            "warn",
            "Not done yet: the Executive doesn't know your company.",
            "Run the setup interview: describe your business, then check what the Executive drafts.",
            link="/onboard",
        )
    return _result("company", "ok", f"Set up for {_clip(profile.name)}.")


def check_owner(snap: Snapshot) -> SetupCheck:
    owner = snap.principal
    if owner is None:
        return _result(
            "owner",
            "warn",
            "Nobody on the team list is marked as the owner.",
            "Finish the setup interview — it asks who the owner is.",
            link="/onboard",
        )
    if snap.local_login:
        return _result("owner", "ok", f"{owner.full_name} is the owner. Local login signs you in as them.")
    if not owner.email:
        return _result(
            "owner",
            "warn",
            f"{owner.full_name} is the owner, but has no email on the team list, so the app won't recognise "
            "them when they sign in — owner-only actions will be refused.",
            "Add the Google address they sign in with to their entry on the People page.",
            link="/people",
        )
    return _result("owner", "ok", f"{owner.full_name} ({owner.email}) is the owner.")


def check_exec_email(snap: Snapshot) -> SetupCheck:
    address = snap.settings.exec_email_address.strip()
    fix = f"Set EXEC_EMAIL_ADDRESS in .env to the Gmail address the Executive sends from, {_RESTART}"
    if "@" not in address:
        return _result("exec_email", "error", "EXEC_EMAIL_ADDRESS isn't an email address.", fix)
    if is_example_email(address):
        return _result(
            "exec_email",
            "warn",
            f"EXEC_EMAIL_ADDRESS is still the sample address {address}. Chat works without a real "
            "one; email doesn't.",
            fix,
        )
    return _result("exec_email", "ok", f"The Executive sends email as {address}.")


def check_api_protection(snap: Snapshot) -> SetupCheck:
    if os.environ.get("BACKEND_SHARED_SECRET", "").strip():
        return _result("api_secret", "ok", "Only the web app can use the API: BACKEND_SHARED_SECRET is set.")
    if snap.local_login:
        return _result(
            "api_secret",
            "ok",
            "Not needed: with local login the API only answers requests addressed to this computer.",
        )
    return _result(
        "api_secret",
        "warn",
        "The API has no shared secret, so anything that can reach it can use it.",
        "Fine on your own computer. On a server, set BACKEND_SHARED_SECRET to the same random value for both "
        "apps (openssl rand -hex 32), then restart them.",
    )


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


async def _fetch(
    http: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> tuple[int, dict[str, Any]] | None:
    """``(status, JSON object or {})``, or ``None`` when the service couldn't
    be reached. Never raises, and never logs the URL, which can hold a token
    (Telegram's does). httpx's own per-request log line would print it at
    INFO; api.main._configure_logging holds the ``httpx`` logger at WARNING."""
    try:
        response = await asyncio.wait_for(http.request(method, url, **kwargs), PROBE_TIMEOUT_S)
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.info("setup status: %s probe failed (%s)", urlsplit(url).hostname, type(exc).__name__)
        return None
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


@dataclass(frozen=True)
class _Roster:
    """Which Person field a channel matches senders on, and what to call it."""

    field: str
    id_name: str


def _turned_away_sender(snap: Snapshot, last: AuditEvent | None, roster: _Roster) -> str | None:
    """The sender of a message the channel turned away in the last day, when
    they still aren't on the team list — ``""`` when the audit row doesn't
    name them. ``None`` when there is nothing left to fix."""
    if last is None or not last.summary.startswith("Rejected"):
        return None
    try:
        at = datetime.fromisoformat(last.ts)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if (snap.now - at).total_seconds() >= _RECENT_FAILURE_S:
        return None
    match = _REJECTED_SENDER_RE.search(last.summary)
    if match is None:
        return ""
    sender = match.group(1)
    rostered = any(str(getattr(person, roster.field) or "") == sender for person in snap.people)
    return None if rostered else sender


def _channel_ready(
    snap: Snapshot, check_id: str, summary: str, *, actor: str, roster: _Roster | None
) -> SetupCheck:
    """The last word on a channel that is connected: can anyone reach the
    Executive through it? Slack, Discord and Telegram answer only people on
    the team list whose entry carries their id on that service."""
    last = snap.last_inbound.get(actor)
    last_at = last.ts if last is not None else None
    if roster is not None:
        if not any(getattr(person, roster.field) for person in snap.people):
            return _result(
                check_id,
                "warn",
                f"{summary} But nobody on the team list has a {roster.id_name}, so every message is ignored.",
                f"Add your {roster.id_name} to your entry on the People page.",
                link="/people",
                last_activity=last_at,
            )
        sender = _turned_away_sender(snap, last, roster)
        if sender is not None:
            named = f" (from {roster.id_name} {sender})" if sender else ""
            return _result(
                check_id,
                "warn",
                f"{summary} The last message{named} was ignored because its sender isn't on the team list.",
                f"If that was you, add that {roster.id_name} to your entry on the People page.",
                link="/people",
                last_activity=last_at,
            )
    return _result(check_id, "ok", summary, last_activity=last_at)


async def check_slack(snap: Snapshot, http: httpx.AsyncClient) -> SetupCheck:
    bot = (snap.settings.slack_bot_token or "").strip()
    app_token = (snap.settings.slack_app_token or "").strip()
    if not bot and not app_token:
        return _result(
            "slack",
            "off",
            "Not set up.",
            f"To use Slack, add SLACK_BOT_TOKEN and SLACK_APP_TOKEN to .env, {_RESTART}",
        )
    if bot in EXAMPLE_VALUES or app_token in EXAMPLE_VALUES:
        return _result(
            "slack",
            "error",
            "Slack still has the sample tokens from .env.example.",
            "Put your Slack app's tokens in SLACK_BOT_TOKEN and SLACK_APP_TOKEN, or delete both lines if you "
            f"don't use Slack, {_RESTART}",
        )
    if not bot.startswith("xoxb-"):
        return _result(
            "slack",
            "error",
            "SLACK_BOT_TOKEN is missing or isn't a bot token — those start with xoxb-.",
            "Copy the Bot User OAuth Token from your Slack app's OAuth & Permissions page into .env, "
            f"{_RESTART}",
        )
    if app_token and not app_token.startswith("xapp-"):
        return _result(
            "slack",
            "error",
            "SLACK_APP_TOKEN isn't an app-level token — those start with xapp-.",
            "Create an app-level token with the connections:write scope on your Slack app's Basic "
            f"Information page, put it in .env, {_RESTART}",
        )

    probe = await _fetch(
        http, "POST", "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {bot}"}
    )
    if probe is None:
        return _result("slack", "warn", "Couldn't reach Slack to test the tokens.", _OFFLINE_FIX)
    body = probe[1]
    if body.get("ok") is not True:
        code = str(body.get("error", ""))
        shown = f" ({code})" if _SLACK_ERROR_CODE_RE.fullmatch(code) else ""
        return _result(
            "slack",
            "error",
            f"Slack turned down SLACK_BOT_TOKEN{shown}.",
            f"Copy the Bot User OAuth Token again from your Slack app's settings into .env, {_RESTART}",
        )
    workspace = _clip(str(body.get("team") or "your Slack workspace"))
    if not app_token:
        return _result(
            "slack",
            "warn",
            f"Can send to {workspace}, but won't hear messages: SLACK_APP_TOKEN isn't set.",
            "Turn on Socket Mode in your Slack app, add its app-level token (starts with xapp-) to .env, "
            f"{_RESTART}",
        )
    if snap.slack_handler is None:
        return _result(
            "slack",
            "error",
            f"The tokens work for {workspace}, but the Slack listener didn't start.",
            f'The API log says why — look for "Failed to start Slack bot". Fix that, {_RESTART}',
        )
    try:
        listening = bool(
            await asyncio.wait_for(snap.slack_handler.client.is_connected(), PROBE_TIMEOUT_S)
        )
    except Exception as exc:  # a client state we can't read counts as not listening
        logger.info("setup status: Slack connection check failed (%s)", type(exc).__name__)
        listening = False
    if not listening:
        return _result(
            "slack",
            "warn",
            f"The tokens work for {workspace}, but Slack isn't delivering messages to the app yet.",
            "If this lasts more than a minute, check SLACK_APP_TOKEN and that Socket Mode is on in "
            "your Slack app's settings.",
        )
    return _channel_ready(
        snap,
        "slack",
        f"Connected to {workspace} and listening.",
        actor="slack",
        roster=_Roster("slack_user_id", "Slack member ID"),
    )


# How a stopped discord.py client names what went wrong, and what to do.
_DISCORD_STOP_FIXES: dict[str, tuple[str, str]] = {
    "LoginFailure": (
        "Discord turned down DISCORD_BOT_TOKEN.",
        f"Reset the token on the Bot tab of the Discord developer portal, paste it into .env, {_RESTART}",
    ),
    "PrivilegedIntentsRequired": (
        "Discord refused the bot: it needs the Message Content intent.",
        f"Turn on Message Content Intent on the Bot tab of the Discord developer portal, {_RESTART}",
    ),
}


async def check_discord(snap: Snapshot, http: httpx.AsyncClient) -> SetupCheck:
    token = (snap.settings.discord_bot_token or "").strip()
    if not token:
        return _result(
            "discord",
            "off",
            "Not set up.",
            f"To use Discord, add DISCORD_BOT_TOKEN and DISCORD_APP_ID to .env, {_RESTART}",
        )

    bot, task = snap.discord_bot, snap.discord_bot_task
    if task is not None and task.done():
        stopped_by = None if task.cancelled() else task.exception()
        kind = type(stopped_by).__name__ if stopped_by is not None else ""
        if kind in _DISCORD_STOP_FIXES:
            return _result("discord", "error", *_DISCORD_STOP_FIXES[kind])
        return _result(
            "discord",
            "error",
            f"The Discord bot stopped{f' ({kind})' if kind else ''}.",
            f"The API log has the details. Fix that, {_RESTART}",
        )
    if bot is None:
        return _result(
            "discord",
            "error",
            "The Discord bot didn't start.",
            f'The API log says why — look for "Discord bot". Fix that, {_RESTART}',
        )
    if bot.is_ready():
        # discord.py stays "ready" through a dropped connection while it
        # reconnects; only the gateway socket says whether messages arrive.
        name = _clip(str(bot.user)) if bot.user is not None else "the bot"
        if not getattr(bot.ws, "open", False):
            return _result(
                "discord",
                "warn",
                f"Signed in as {name}, but the connection to Discord dropped and the bot is "
                "reconnecting.",
                "Check again in a minute. If it stays like this, check this computer's internet connection "
                "and the API log.",
            )
        return _channel_ready(
            snap,
            "discord",
            f"Connected as {name}.",
            actor="discord",
            roster=_Roster("discord_user_id", "Discord user ID"),
        )

    # Still connecting: test the token, so a bad one is named now rather than
    # whenever discord.py gives up.
    probe = await _fetch(
        http, "GET", "https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"}
    )
    if probe is None:
        return _result("discord", "warn", "Couldn't reach Discord to test the token.", _OFFLINE_FIX)
    if probe[0] == 401:
        return _result("discord", "error", *_DISCORD_STOP_FIXES["LoginFailure"])
    return _result(
        "discord",
        "warn",
        "Still connecting to Discord.",
        "Check again in a minute. If it stays like this, restart the app and read the API log.",
    )


_TELEGRAM_REREGISTER = (
    "Set TELEGRAM_WEBHOOK_SECRET, restart the app, and register the webhook again with the same secret "
    "(docs/telegram_setup.md)."
)


async def check_telegram(snap: Snapshot, http: httpx.AsyncClient) -> SetupCheck:
    settings = snap.settings
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        return _result("telegram", "off", "Not set up.", "To use Telegram, follow docs/telegram_setup.md.")
    # Checked before the token goes into a URL, so it can't reshape one.
    if not _TELEGRAM_TOKEN_RE.fullmatch(token):
        return _result(
            "telegram",
            "error",
            "TELEGRAM_BOT_TOKEN isn't a Telegram bot token — those are digits, a colon, then letters.",
            f"Copy the token @BotFather gave you into .env, {_RESTART}",
        )
    secret = settings.telegram_webhook_secret
    if secret and not settings.telegram_webhook_secret_valid:
        return _result(
            "telegram",
            "error",
            "TELEGRAM_WEBHOOK_SECRET has characters Telegram won't accept, so the app turns "
            "away every message.",
            "Use 1–256 letters, digits, _ or - (openssl rand -hex 32 makes one), restart the app, "
            "and register the webhook again with it (docs/telegram_setup.md).",
        )
    if snap.local_login and not secret:
        return _result(
            "telegram",
            "error",
            "Local login turns away Telegram messages unless TELEGRAM_WEBHOOK_SECRET is set.",
            _TELEGRAM_REREGISTER,
        )

    base = f"https://api.telegram.org/bot{token}"
    me = await _fetch(http, "GET", f"{base}/getMe")
    if me is None:
        return _result("telegram", "warn", "Couldn't reach Telegram to test the token.", _OFFLINE_FIX)
    if me[1].get("ok") is not True:
        return _result(
            "telegram",
            "error",
            "Telegram turned down TELEGRAM_BOT_TOKEN.",
            f"Ask @BotFather for the token again (/token), paste it into .env, {_RESTART}",
        )
    me_result = me[1].get("result")
    username = me_result.get("username") if isinstance(me_result, dict) else None
    bot_name = f"@{_clip(str(username))}" if username else "The bot"

    hook = await _fetch(http, "GET", f"{base}/getWebhookInfo")
    if hook is None:
        return _result(
            "telegram",
            "warn",
            f"{bot_name} works, but Telegram didn't say where it delivers messages.",
            _OFFLINE_FIX,
        )
    info = hook[1].get("result")
    if not isinstance(info, dict):
        info = {}
    url = str(info.get("url") or "")
    if not url:
        return _result(
            "telegram",
            "warn",
            f"{bot_name} works, but Telegram doesn't know where to deliver its messages.",
            "Register this app's /webhook/telegram address with setWebhook — see docs/telegram_setup.md.",
        )
    if not urlsplit(url).path.endswith("/webhook/telegram"):
        return _result(
            "telegram",
            "warn",
            f"Telegram delivers {bot_name}'s messages to an address that isn't this app's /webhook/telegram.",
            "Register the webhook again with this app's address — see docs/telegram_setup.md.",
        )
    error_at = info.get("last_error_date")
    if isinstance(error_at, int) and snap.now.timestamp() - error_at < _RECENT_FAILURE_S:
        reason = _clip(str(info.get("last_error_message") or "no reason given"))
        return _result(
            "telegram",
            "warn",
            f"Telegram couldn't deliver {bot_name}'s last message: {reason}.",
            "A 401 means the secret registered with setWebhook doesn't match "
            "TELEGRAM_WEBHOOK_SECRET — register the webhook again (docs/telegram_setup.md). "
            "Otherwise, check this app can be reached at that address.",
        )
    if not secret:
        return _result(
            "telegram",
            "warn",
            f"{bot_name} is receiving messages, but anyone who finds the webhook address can send fake ones: "
            "TELEGRAM_WEBHOOK_SECRET isn't set.",
            _TELEGRAM_REREGISTER,
        )
    return _channel_ready(
        snap,
        "telegram",
        f"{bot_name} is receiving messages.",
        actor="telegram",
        roster=_Roster("telegram_chat_id", "Telegram chat ID"),
    )


def _readable_service_account(path: str) -> bool:
    try:
        with Path(path).expanduser().open("rb") as handle:
            raw = handle.read(_SERVICE_ACCOUNT_KEY_MAX_BYTES + 1)
        if len(raw) > _SERVICE_ACCOUNT_KEY_MAX_BYTES:
            return False
        key = json.loads(raw)
    except (OSError, ValueError):
        return False
    return isinstance(key, dict) and bool(key.get("client_email"))


def check_google_chat(snap: Snapshot) -> SetupCheck:
    settings = snap.settings
    project = (settings.google_chat_project_number or "").strip()
    key_file = (settings.google_chat_service_account_file or "").strip()
    key_email = (settings.google_chat_service_account_email or "").strip()
    if not (project or key_file or key_email):
        return _result(
            "google_chat", "off", "Not set up.", "To use Google Chat, follow docs/google_chat_setup.md."
        )
    if not re.fullmatch(r"[0-9]+", project):
        return _result(
            "google_chat",
            "error",
            "GOOGLE_CHAT_PROJECT_NUMBER needs your Google Cloud project's number — digits only, "
            "not the project ID.",
            f"Copy the project number from the Google Cloud console's dashboard into .env, {_RESTART}",
        )
    if not (key_file or key_email):
        return _result(
            "google_chat",
            "error",
            "Google Chat has no service account to reply with.",
            "Set GOOGLE_CHAT_SERVICE_ACCOUNT_FILE or GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL in .env — "
            f"docs/google_chat_setup.md says which — {_RESTART}",
        )
    if key_file and not _readable_service_account(key_file):
        return _result(
            "google_chat",
            "error",
            "Can't read a service-account key from the file GOOGLE_CHAT_SERVICE_ACCOUNT_FILE names.",
            f"Point it at the full path of the JSON key you downloaded from Google Cloud, {_RESTART}",
        )
    return _channel_ready(
        snap,
        "google_chat",
        "Set up. Google Chat delivers messages to this app's /webhook/google-chat address "
        "(this page can't test that part).",
        actor="google_chat",
        roster=None,
    )


def _google_sign_in_missing() -> str | None:
    """The Google credentials the workspace-mcp child can't sign in without,
    named for the fix, or None. Read from this process's environment because
    that is what the MCP gateway forwards to it."""
    def have(key: str) -> bool:
        return bool(os.environ.get(key, "").strip())

    if (os.environ.get("GWORKSPACE_AUTH_MODE", "").strip() or "oauth") == "service_account":
        if have("GOOGLE_SERVICE_ACCOUNT_KEY_JSON") or have("GOOGLE_SERVICE_ACCOUNT_KEY_FILE"):
            return None
        return "GOOGLE_SERVICE_ACCOUNT_KEY_JSON or GOOGLE_SERVICE_ACCOUNT_KEY_FILE"
    if have("GOOGLE_OAUTH_CLIENT_ID") and have("GOOGLE_OAUTH_CLIENT_SECRET"):
        return None
    return "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET"


def check_gmail(snap: Snapshot) -> SetupCheck:
    from openexecutive.orchestrator.mcp_gateway import configured_server_names

    settings = snap.settings
    servers = configured_server_names(settings.mcp_servers_config_path) if settings.mcp_enabled else []
    if "google_workspace" not in servers:
        return _result(
            "gmail",
            "off",
            "Not set up: the Executive can't read or send email.",
            "To connect Gmail, follow the Google Workspace steps in .env.example.",
        )
    if snap.mcp_gateway is None:
        return _result(
            "gmail",
            "error",
            "The connection to Google didn't start, so email is off.",
            f'The API log says why — look for "MCP gateway". Fix that, {_RESTART}',
        )
    missing = _google_sign_in_missing()
    if missing:
        return _result(
            "gmail",
            "error",
            f"Google can't sign the Executive in: {missing} isn't set.",
            f"Set it in .env (the Google Workspace steps there explain how), {_RESTART}",
        )
    summary = (
        f"Connected. The Executive checks {settings.exec_email_address}'s inbox every "
        f"{settings.email_poll_interval_seconds} seconds."
    )
    if snap.last_inbound.get("email") is None:
        summary += (
            " No email has come in yet — if you've sent one, sign Google in once as that address "
            "(scripts/mint-google-token.py)."
        )
    return _channel_ready(snap, "gmail", summary, actor="email", roster=None)


# ---------------------------------------------------------------------------
# What runs in the background
# ---------------------------------------------------------------------------


def check_scheduler(snap: Snapshot) -> SetupCheck:
    from openexecutive.scheduler.pause import get_pause_state
    from openexecutive.scheduler.runner import scheduler_heartbeat

    settings = snap.settings
    if not settings.scheduler_enabled:
        return _result(
            "scheduler",
            "off",
            "Turned off, so no briefs, reminders or follow-ups go out.",
            f"Set SCHEDULER_ENABLED=true in .env, {_RESTART}",
        )
    # Liveness first: a paused scheduler still ticks, so one that has
    # stopped needs a restart, not the Resume button.
    started_at, last_tick = scheduler_heartbeat()
    restart_fix = 'Restart the app, and look in the API log for lines from "scheduler".'
    if started_at is None:
        return _result(
            "scheduler",
            "error",
            "The scheduler isn't running, so briefs and follow-ups won't go out.",
            restart_fix,
        )
    if last_tick is None:
        waited = (snap.now - started_at).total_seconds()
        if waited < _SCHEDULER_STARTUP_GRACE_S:
            return _result("scheduler", "warn", "Starting up.", "Check again in a minute.")
        return _result(
            "scheduler",
            "error",
            f"The scheduler started {_ago(waited)} but hasn't finished a check since.",
            restart_fix,
        )
    ticked_at, outcome = last_tick
    silent_for = (snap.now - ticked_at).total_seconds()
    stale_after = max(
        _SCHEDULER_STALE_TICKS * settings.scheduler_poll_interval_seconds, _SCHEDULER_STALE_FLOOR_S
    )
    if silent_for > stale_after:
        return _result(
            "scheduler",
            "error",
            f"The scheduler has stopped: its last check was {_ago(silent_for)}.",
            restart_fix,
        )
    if outcome == "failed":
        return _result(
            "scheduler",
            "error",
            "The scheduler's last check hit an error.",
            'Look in the API log for "scheduler tick failed" to see why.',
        )
    if get_pause_state().paused:
        return _result(
            "scheduler",
            "warn",
            "Paused: briefs, reminders and email checks wait until you resume.",
            "Press Resume in the banner at the top of the page.",
        )
    if outcome == "waiting_for_company":
        return _result(
            "scheduler",
            "warn",
            "Waiting for company setup: nothing scheduled runs until the setup interview is done.",
            "Finish the setup interview.",
            link="/onboard",
        )
    return _result("scheduler", "ok", "Running.")


async def check_memory(snap: Snapshot) -> SetupCheck:
    from openexecutive.api.routes.health import honcho_health

    probe = await honcho_health()
    status = probe.get("status")
    if status == "disabled":
        return _result(
            "memory",
            "off",
            "Off. This optional service remembers people across conversations.",
            f"To turn it on, set HONCHO_ENABLED=true and HONCHO_API_KEY in .env, {_RESTART}",
        )
    if status == "ok":
        return _result("memory", "ok", "Connected to Honcho.")
    return _result(
        "memory",
        "error",
        f"Can't reach Honcho ({probe.get('error_type', 'unknown error')}).",
        f"Check HONCHO_API_KEY and HONCHO_BASE_URL in .env, {_RESTART}",
    )


# ---------------------------------------------------------------------------
# Running them all
# ---------------------------------------------------------------------------

_INBOUND_ACTORS = ("slack", "discord", "telegram", "google_chat", "email")


def _latest_inbound() -> dict[str, AuditEvent | None]:
    from openexecutive.audit import get_audit_logger

    audit = get_audit_logger()
    latest: dict[str, AuditEvent | None] = {}
    for actor in _INBOUND_ACTORS:
        rows = audit.query(event_type="integration_inbound", actor=actor, limit=1)
        latest[actor] = rows[0] if rows else None
    return latest


def gather_snapshot(settings: Settings, *, local_login: bool, app_state: Any) -> Snapshot:
    """Read everything the checks need. Blocking (SQLite): run it off the loop."""
    from openexecutive.people.store import find_principal_person, list_people

    return Snapshot(
        settings=settings,
        now=datetime.now(UTC),
        local_login=local_login,
        people=list_people(),
        principal=find_principal_person(),
        last_inbound=_latest_inbound(),
        discord_bot=getattr(app_state, "discord_bot", None),
        discord_bot_task=getattr(app_state, "discord_bot_task", None),
        slack_handler=getattr(app_state, "slack_handler", None),
        mcp_gateway=getattr(app_state, "mcp_gateway", None),
    )


def _check_runners(
    snap: Snapshot, http: httpx.AsyncClient
) -> dict[str, Callable[[], Awaitable[SetupCheck]]]:
    def off_loop(check: Callable[[Snapshot], SetupCheck]) -> Callable[[], Awaitable[SetupCheck]]:
        # The synchronous checks read files and SQLite.
        return lambda: asyncio.to_thread(check, snap)

    return {
        "ai_model": lambda: check_ai_model(snap),
        "company": off_loop(check_company),
        "owner": off_loop(check_owner),
        "exec_email": off_loop(check_exec_email),
        "api_secret": off_loop(check_api_protection),
        "gmail": off_loop(check_gmail),
        "slack": lambda: check_slack(snap, http),
        "discord": lambda: check_discord(snap, http),
        "telegram": lambda: check_telegram(snap, http),
        "google_chat": off_loop(check_google_chat),
        "scheduler": off_loop(check_scheduler),
        "memory": lambda: check_memory(snap),
    }


async def run_checks(snap: Snapshot, http: httpx.AsyncClient) -> list[SetupCheck]:
    """Every check at once, in ``LABELS`` order. A check that crashes turns
    red on its own instead of taking the page down."""
    runners = _check_runners(snap, http)

    async def guarded(check_id: str) -> SetupCheck:
        try:
            return await runners[check_id]()
        except Exception as exc:
            logger.warning("setup status: the %s check crashed (%s)", check_id, type(exc).__name__)
            return _result(
                check_id,
                "error",
                "This check couldn't run.",
                'The API log has the details — look for "setup status".',
            )

    return list(await asyncio.gather(*(guarded(check_id) for check_id in LABELS)))
