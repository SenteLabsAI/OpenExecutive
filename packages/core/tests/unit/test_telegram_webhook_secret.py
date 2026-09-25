"""TELEGRAM_WEBHOOK_SECRET verifies an update only when Telegram could send it.

setWebhook accepts a secret of 1–256 ``A-Z a-z 0-9 _ -``, so any other value
— a ``# note`` left in .env, a pasted ``<value from Step 2>`` — can only be
matched by someone who guessed it. With such a secret the webhook refuses
every update, and nothing treats a Telegram message as verified.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openexecutive.api.main import _webhook_verifies_its_caller, create_app
from openexecutive.config import get_settings

TOKEN = "123456789:AAH" + "x" * 32
HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture(autouse=True)
def _telegram_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)


def _post(secret: str, header: str | None) -> int:
    headers = {HEADER: header} if header is not None else {}
    # An empty update: past the secret check it is ignored with a 200.
    return TestClient(create_app()).post("/webhook/telegram", json={}, headers=headers).status_code


@pytest.mark.parametrize(
    ("secret", "valid"),
    [
        ("", False),
        ("hook-secret_42", True),
        ("x" * 256, True),
        ("x" * 257, False),
        ("# from step 2", False),
        ("<value from Step 2>", False),
        ("sécret", False),
    ],
)
def test_only_a_secret_telegram_can_send_is_valid(
    monkeypatch: pytest.MonkeyPatch, secret: str, valid: bool
) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", secret)
    assert get_settings().telegram_webhook_secret_valid is valid
    assert _webhook_verifies_its_caller("/webhook/telegram") is valid


def test_a_valid_secret_admits_only_the_matching_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    assert _post("hook-secret", "hook-secret") == 200
    assert _post("hook-secret", "guess") == 401
    assert _post("hook-secret", None) == 401


@pytest.mark.parametrize("secret", ["# from step 2", "<value from Step 2>", "sécret"])
def test_a_secret_telegram_cannot_send_refuses_even_a_matching_header(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    # A comment left as the value is kept rather than read as unset (which
    # would switch the check off), and matching it proves only a guess.
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", secret)
    assert get_settings().telegram_webhook_secret == secret
    header = secret if secret.isascii() else "guess"
    assert _post(secret, header) == 401


def test_no_secret_still_accepts_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unchanged: without a secret the webhook can't tell Telegram from anyone
    # else, which Setup status reports amber.
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert _post("", None) == 200
