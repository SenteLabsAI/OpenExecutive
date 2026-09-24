#!/usr/bin/env python3
"""Mint the workspace-mcp single-user OAuth credential locally (one-time).

workspace-mcp has no `auth` subcommand: the OAuth flow fires on the first
authenticated tool call. This spawns the server over stdio and triggers that
call so your browser opens for consent; the refresh-token credential is written
to WORKSPACE_MCP_CREDENTIALS_DIR. Copy that file onto the API volume afterward,
into /data/google_credentials/ (see "Google Workspace credentials" in docs/deployment.md).

Run in a shell where the OAuth client is exported:

    export GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    export USER_GOOGLE_EMAIL=exec@you.com        # the exec account to sign in as
    uv run --with mcp python scripts/mint-google-token.py

Credentials land in ./.gworkspace-credentials/ unless WORKSPACE_MCP_CREDENTIALS_DIR
is set. Do NOT commit that file — it is the live refresh token.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time


def _need(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        sys.exit(f"error: {key} must be exported in this shell first")
    return val


async def _run() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _need("GOOGLE_OAUTH_CLIENT_ID")
    _need("GOOGLE_OAUTH_CLIENT_SECRET")

    cred_dir = pathlib.Path(
        os.environ.setdefault(
            "WORKSPACE_MCP_CREDENTIALS_DIR",
            str(pathlib.Path.cwd() / ".gworkspace-credentials"),
        )
    )
    cred_dir.mkdir(parents=True, exist_ok=True)
    email = os.environ.get("USER_GOOGLE_EMAIL") or os.environ.get("EXEC_EMAIL_ADDRESS")

    child_env = os.environ.copy()
    # The OAuth callback comes back over http://localhost:8000 — allow it.
    child_env.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    params = StdioServerParameters(
        command="uvx",
        args=["workspace-mcp", "--single-user", "--tool-tier", "complete"],
        env=child_env,
    )

    print(f"• credentials dir : {cred_dir}")
    print(f"• sign in as      : {email or '(unset — set USER_GOOGLE_EMAIL)'}")
    print("• starting workspace-mcp over stdio …")

    before = {p.name for p in cred_dir.glob("*.json")}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            by_name = {t.name: t for t in tools}

            # Prefer the explicit auth tool; else any auth-requiring read tool.
            trigger = next(
                (
                    n
                    for n in (
                        "start_google_auth",
                        "list_calendars",
                        "get_events",
                        "list_gmail_labels",
                        "search_gmail_messages",
                    )
                    if n in by_name
                ),
                None,
            )
            if trigger is None:
                sys.exit("error: no auth-triggering tool found; tools=" + ", ".join(sorted(by_name)))

            schema = by_name[trigger].inputSchema or {}
            props = schema.get("properties", {})
            required = schema.get("required", [])
            args: dict[str, object] = {}
            if "user_google_email" in props and email:
                args["user_google_email"] = email
            for field in required:  # fill any other required field minimally
                if field in args:
                    continue
                t = props.get(field, {}).get("type")
                args[field] = (
                    (email or "")
                    if field == "user_google_email"
                    else "Open Executive"
                    if t == "string"
                    else 1
                    if t in ("integer", "number")
                    else False
                    if t == "boolean"
                    else None
                )

            print(f"• triggering OAuth via '{trigger}' — a browser should open…\n")
            try:
                res = await session.call_tool(trigger, args)
                for c in res.content:
                    if getattr(c, "text", None):
                        print(c.text)
            except Exception as exc:  # noqa: BLE001 — surface, then keep waiting
                print(f"(tool call returned: {exc})")

            print("\n→ Complete the Google sign-in in your browser as the exec account.")
            print("  Waiting for the credential file (up to 5 min)…")
            deadline = time.time() + 300
            while time.time() < deadline:
                new = [p for p in cred_dir.glob("*.json") if p.name not in before]
                if new:
                    f = new[0]
                    print(f"\n✓ credential written: {f}  ({f.stat().st_size} bytes)")
                    print("Done — leave it there and tell me; I'll upload it to the API.")
                    return 0
                await asyncio.sleep(2)
            print("\nTimed out waiting for the credential — did the sign-in finish?", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
