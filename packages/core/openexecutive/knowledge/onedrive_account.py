"""The OneDrive sync's access token: the Executive's own Microsoft 365 sign-in.

Google Drive's sync reads as a service account that only sees the folders
shared with it. Microsoft has no such thing short of app-only access, which
covers every file in the tenant. So the OneDrive sync reads as the account the
microsoft_365 MCP server is already signed in as, and limits itself to the
folders listed in ``ONEDRIVE_SYNC_FOLDERS``.

That sign-in lives in the server's own encrypted MSAL cache. Rather than parse
it here, the sync asks the server's launcher for a token
(``ms365-mcp-launch.sh --access-token``), which runs
``docker/ms365-access-token.mjs`` with the launcher's environment through the
server's own AuthManager: same cache, same key, same pinned account. The
helper prints ``{"access_token", "expires_on"}`` on stdout and nothing else;
the token is held in memory for one sync tick and only ever sent to Graph.
Microsoft issues one Graph token per sign-in, so although the sync asks for
``Files.Read.All`` the token carries every Graph permission the sign-in was
granted (mail and calendar included); that is why it never leaves this
process except to Graph, and why the helper gets only the launcher's
environment.

``onedrive_token_provider`` is the seam ``onedrive_sync`` calls. Its errors
carry a short reason (an MSAL error code, never a token) that the sync shows
on the Knowledge page as the source's last error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from openexecutive.knowledge.onedrive_client import ONEDRIVE_SCOPES

if TYPE_CHECKING:
    from openexecutive.config import Settings

logger = logging.getLogger(__name__)

TokenProvider = Callable[[], Awaitable[str]]
_TIMEOUT_S = 60.0
_TOKEN_MARGIN_S = 120.0
# The helper's last stderr line is an MSAL error code and message. Kept
# short and only ever logged or shown as a reason, never parsed for a token.
_REASON_CHARS = 300


class OneDriveCredentialMissing(Exception):
    """No usable Microsoft sign-in for OneDrive: the launcher is missing,
    nobody has signed in, or the sign-in doesn't allow reading files."""


class OneDriveAuthTransient(Exception):
    """Getting a token failed in a way worth retrying on the next tick."""


# MSAL error codes that mean the sign-in itself must be redone, not retried.
_RECONNECT_CODES = (
    "invalid_grant",
    "interaction_required",
    "consent_required",
    "no_account",
    "No valid token",
    "unexpected account",
)


# What the launcher and Node need, and nothing else of the API's environment
# (no API keys or shared secrets reach the helper).
_HELPER_ENV_VARS = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
)


def _helper_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _HELPER_ENV_VARS if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("MS365_")})
    return env


async def _run_helper(launcher: str, scopes: tuple[str, ...]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        launcher,
        "--access-token",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_helper_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps({"scopes": list(scopes)}).encode()),
            timeout=_TIMEOUT_S,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise OneDriveAuthTransient("getting a Microsoft token timed out") from None
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def launcher_token_provider(
    launcher: str,
    *,
    scopes: tuple[str, ...] = ONEDRIVE_SCOPES,
    run: Callable[[str, tuple[str, ...]], Awaitable[tuple[int, str, str]]] = _run_helper,
    clock: Callable[[], float] = time.time,
) -> TokenProvider:
    """An access-token source backed by the Microsoft 365 launcher.

    Raises ``OneDriveCredentialMissing`` now when the launcher isn't there,
    so a tick stops before listing anything. The returned callable asks the
    helper at most once per token lifetime; once the sign-in is refused it
    keeps raising for the rest of this provider's life (one tick)."""
    if not launcher or not os.path.isfile(launcher) or not os.access(launcher, os.X_OK):
        raise OneDriveCredentialMissing("the Microsoft 365 launcher is not installed")

    lock = asyncio.Lock()
    cached: dict[str, object] = {"token": None, "expires": 0.0, "refused": None}

    async def _fetch() -> str:
        try:
            code, out, err = await run(launcher, scopes)
        except (OSError, OneDriveAuthTransient) as exc:
            raise OneDriveAuthTransient(f"getting a Microsoft token failed ({exc})") from None
        reason = (err.strip().splitlines() or ["no output"])[-1][:_REASON_CHARS]
        if code != 0:
            if any(marker in reason for marker in _RECONNECT_CODES):
                raise OneDriveCredentialMissing(f"Microsoft refused the saved sign-in: {reason}")
            raise OneDriveAuthTransient(f"getting a Microsoft token failed: {reason}")
        try:
            body = json.loads(out)
        except ValueError:
            raise OneDriveAuthTransient("the token helper printed something unreadable") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise OneDriveAuthTransient("the token helper returned no token")
        expires_on = body.get("expires_on") if isinstance(body, dict) else None
        expires = float(expires_on) if isinstance(expires_on, int | float) else clock() + 600
        cached["token"] = token
        cached["expires"] = expires - _TOKEN_MARGIN_S
        return token

    async def token() -> str:
        async with lock:
            refused = cached["refused"]
            if isinstance(refused, str):
                raise OneDriveCredentialMissing(refused)
            current = cached["token"]
            if isinstance(current, str) and clock() < float(cached["expires"]):  # type: ignore[arg-type]
                return current
            try:
                fresh = await _fetch()
            except OneDriveCredentialMissing as exc:
                cached["refused"] = str(exc)
                logger.warning("onedrive_account: %s", exc)
                raise
            except OneDriveAuthTransient as exc:
                logger.warning("onedrive_account: %s", exc)
                raise
            return fresh

    return token


def onedrive_token_provider(settings: Settings) -> TokenProvider:
    """The token source ``run_onedrive_sync`` reads with."""
    return launcher_token_provider(settings.ms365_mcp_launcher)
