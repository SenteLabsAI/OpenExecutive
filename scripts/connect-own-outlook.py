#!/usr/bin/env python3
"""Connect YOUR OWN Outlook mailbox for Act as me (one-time, per person).

The Outlook twin of scripts/connect-own-gmail.py. Act as me lets the Executive
write drafts as you, in your own mailbox — never from its own account, and
never through the MCP gateway the model can reach. This mints the credential
it uses: a Microsoft sign-in as you (device code: open the link it prints and
type the code, from any browser), asking only for your profile and mail
(read, write drafts, and send the drafts you approve), and writes one file
named from a hash of your address. A person has one mailbox for Act as me:
this replaces a Gmail file for the same address.

It uses the Executive's own Entra app (the one the Microsoft 365 setup in
.env.example registers; "Allow public client flows" must be on). Its delegated
Microsoft Graph permissions must include User.Read, Mail.ReadWrite, Mail.Send
and offline_access. A personal Microsoft account needs an app that accepts
personal accounts, with MS365_MCP_TENANT_ID=common or consumers.

    export MS365_MCP_CLIENT_ID=...
    export MS365_MCP_TENANT_ID=...      # optional; default "common"
    uv run python scripts/connect-own-outlook.py --email you@example.com

The file lands in $DELEGATION_GOOGLE_CREDENTIALS_DIR (default:
./delegation-credentials). Copy it onto the API's volume, into the directory
the API's DELEGATION_GOOGLE_CREDENTIALS_DIR points at (Docker:
/data/delegation_google/). No restart is needed — it is read on each use.
Sign in as the address on your People entry: any other account is refused
when it is used. Do NOT commit the file — it holds your refresh token.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName"
SENT_ITEMS = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages"
    "?$orderby=sentDateTime%20desc&$top=10&$select=from,isDraft"
)
GRAPH_SCOPES = ("User.Read", "Mail.ReadWrite", "Mail.Send")
SCOPE = " ".join(["openid", "offline_access", *(f"https://graph.microsoft.com/{s}" for s in GRAPH_SCOPES)])
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# The tenant every personal Microsoft account (Outlook.com, Hotmail) is in.
CONSUMER_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"
TENANT_RE = re.compile(r"common|organizations|consumers|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CREDENTIAL_VERSION = 1


def email_key(email: str) -> str:
    """The file stem for ``email`` — must match delegation.gmail.email_key."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def credential_payload(
    email: str, refresh_token: str, client_id: str, tenant: str, *, personal: bool
) -> dict[str, Any]:
    """The file's content — the format delegation.outlook.load_credential reads."""
    microsoft: dict[str, str] = {"refresh_token": refresh_token, "client_id": client_id, "tenant": tenant}
    return {
        "version": CREDENTIAL_VERSION,
        "provider": "microsoft",
        "email": email.strip().lower(),
        "account": "personal" if personal else "work",
        "scopes": list(GRAPH_SCOPES),
        "microsoft": microsoft,
    }


def write_credential(directory: pathlib.Path, email: str, payload: dict[str, Any]) -> pathlib.Path:
    """Write the file with owner-only permissions (0700 directory, 0600 file)."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{email_key(email)}.json"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def token_tenant(id_token: str) -> str:
    """The signed-in account's tenant id from the sign-in's id token (only
    read to tell a personal account from a work one; Graph checks the rest)."""
    try:
        body = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        return ""
    tid = str(claims.get("tid") or "").lower() if isinstance(claims, dict) else ""
    return tid if TENANT_RE.fullmatch(tid) else ""


def _post(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 — fixed https URL
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except ValueError:
            return exc.code, {}


def _graph(url: str, access_token: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 — fixed https URL
        data = json.load(resp)
    return data if isinstance(data, dict) else {}


def _profile_email(access_token: str) -> str:
    """The mailbox's own address, as delegation.outlook reads it: /me's mail,
    else (a personal account signed in with another address) the address its
    latest sent mail went from, else the sign-in name."""
    me = _graph(GRAPH_ME, access_token)
    if me.get("mail"):
        return str(me["mail"]).strip().lower()
    sent = _graph(SENT_ITEMS, access_token)
    for item in sent.get("value") or []:
        if isinstance(item, dict) and item.get("isDraft") is not True:
            address = ((item.get("from") or {}).get("emailAddress") or {}).get("address")
            if address:
                return str(address).strip().lower()
    return str(me.get("userPrincipalName") or "").strip().lower()


def _sign_in(tenant: str, client_id: str) -> dict[str, Any]:
    status, code = _post(f"{LOGIN_BASE}/{tenant}/oauth2/v2.0/devicecode", {"client_id": client_id, "scope": SCOPE})
    if status != 200 or "device_code" not in code:
        sys.exit(f"error: Microsoft refused the sign-in request: {code.get('error_description') or code.get('error') or status}")
    print(str(code.get("message") or f"Open {code.get('verification_uri')} and enter {code.get('user_code')}."))
    interval = int(code.get("interval") or 5)
    deadline = time.monotonic() + int(code.get("expires_in") or 900)
    form = {"grant_type": DEVICE_GRANT, "client_id": client_id, "device_code": str(code["device_code"])}
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, token = _post(f"{LOGIN_BASE}/{tenant}/oauth2/v2.0/token", form)
        if status == 200:
            return token
        error = str(token.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        sys.exit(f"error: sign-in failed: {token.get('error_description') or error or status}")
    sys.exit("error: the sign-in code expired — run it again.")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--email", required=True, help="Your own Outlook address (as on your People entry)")
    args = parser.parse_args()
    email = args.email.strip().lower()
    if "@" not in email:
        sys.exit("error: --email must be an email address")

    client_id = os.environ.get("MS365_MCP_CLIENT_ID", "").strip()
    if not client_id:
        sys.exit("error: MS365_MCP_CLIENT_ID must be exported in this shell first")
    tenant = (os.environ.get("MS365_MCP_TENANT_ID") or "common").strip().lower()
    if not TENANT_RE.fullmatch(tenant):
        sys.exit("error: MS365_MCP_TENANT_ID must be a tenant id, common, organizations or consumers")

    print(f"Sign in as {email} and allow read, draft and send access to your mail.")
    token = _sign_in(tenant, client_id)
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        sys.exit("error: Microsoft returned no refresh token — check the app allows offline_access.")
    opened = _profile_email(str(token.get("access_token") or ""))
    if opened != email:
        sys.exit(f"error: you signed in as {opened}, not {email}. Run it again as {email}.")
    tid = token_tenant(str(token.get("id_token") or ""))
    personal = tid == CONSUMER_TENANT_ID or tenant == "consumers"
    # Refresh where this account lives: its own tenant, or the consumer one.
    stored_tenant = "consumers" if personal else (tid or tenant)

    directory = pathlib.Path(
        os.environ.get("DELEGATION_GOOGLE_CREDENTIALS_DIR") or pathlib.Path.cwd() / "delegation-credentials"
    )
    path = write_credential(
        directory, email,
        credential_payload(email, refresh_token, client_id, stored_tenant, personal=personal),
    )
    print(f"Saved {path}")
    print("Copy it into the API's DELEGATION_GOOGLE_CREDENTIALS_DIR (Docker: /data/delegation_google/),")
    print("then open Settings → Act as me. Do not commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
