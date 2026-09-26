#!/usr/bin/env python3
"""Connect YOUR OWN Gmail for Act as me (one-time, per person).

Act as me lets the Executive write drafts as you, in your own Gmail — never
from its own account, and never through the MCP gateway the model can reach.
This mints the credential it uses: a browser sign-in as you, asking only for
Gmail read + compose (drafts), and writes one file named from a hash of your
address.

Run it where you can open a browser, with the Executive's Google OAuth client
exported (the same Desktop client scripts/mint-google-token.py uses):

    export GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    uv run --with google-auth-oauthlib python scripts/connect-own-gmail.py --email you@example.com

The file lands in $DELEGATION_GOOGLE_CREDENTIALS_DIR (default:
./delegation-credentials). Copy it onto the API's volume, into the directory
the API's DELEGATION_GOOGLE_CREDENTIALS_DIR points at (Docker:
/data/delegation_google/). No restart is needed — it is read on each use.
Sign in as the address on your People entry: any other account is refused
when it is used. Do NOT commit the file — it holds your refresh token.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import urllib.request
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
CREDENTIAL_VERSION = 1


def email_key(email: str) -> str:
    """The file stem for ``email`` — must match delegation.gmail.email_key."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def credential_payload(email: str, refresh_token: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """The file's content — the format delegation.gmail.load_credential reads."""
    return {
        "version": CREDENTIAL_VERSION,
        "email": email.strip().lower(),
        "scopes": SCOPES,
        "authorized_user": {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "token_uri": TOKEN_URI,
        },
    }


def write_credential(directory: pathlib.Path, email: str, payload: dict[str, Any]) -> pathlib.Path:
    """Write the file with owner-only permissions (0700 directory, 0600 file)."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"{email_key(email)}.json"
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def _need(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        sys.exit(f"error: {key} must be exported in this shell first")
    return value


def _profile_email(access_token: str) -> str:
    request = urllib.request.Request(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 — fixed https URL
        return str(json.load(resp).get("emailAddress") or "").strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True, help="Your own Gmail address (as on your People entry)")
    args = parser.parse_args()
    email = args.email.strip().lower()
    if "@" not in email:
        sys.exit("error: --email must be an email address")

    client_id = _need("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _need("GOOGLE_OAUTH_CLIENT_SECRET")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    print(f"Opening a browser: sign in as {email} and allow read + draft access to Gmail.")
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline", login_hint=email)
    if not creds.refresh_token:
        sys.exit("error: Google returned no refresh token — run it again (it asks for consent each time).")
    opened = _profile_email(creds.token)
    if opened != email:
        sys.exit(f"error: you signed in as {opened}, not {email}. Run it again as {email}.")

    directory = pathlib.Path(
        os.environ.get("DELEGATION_GOOGLE_CREDENTIALS_DIR") or pathlib.Path.cwd() / "delegation-credentials"
    )
    path = write_credential(
        directory, email, credential_payload(email, creds.refresh_token, client_id, client_secret)
    )
    print(f"Saved {path}")
    print("Copy it into the API's DELEGATION_GOOGLE_CREDENTIALS_DIR (Docker: /data/delegation_google/),")
    print("then open Settings → Act as me. Do not commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
