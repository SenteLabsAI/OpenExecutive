"""EMAIL_PROVIDER / CALENDAR_PROVIDER → backend selection, fail-soft."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.config import Settings
from openexecutive.integrations.workspace import (
    CalendarProvider,
    MailProvider,
    get_calendar_provider,
    get_mail_provider,
)
from openexecutive.integrations.workspace.google import GoogleCalendar, GoogleMail
from openexecutive.integrations.workspace.microsoft import MicrosoftCalendar, MicrosoftMail
from openexecutive.integrations.workspace.registry import (
    log_provider_status,
    provider_server_missing,
)


def test_defaults_are_google() -> None:
    assert isinstance(get_mail_provider(SimpleNamespace()), GoogleMail)
    assert isinstance(get_calendar_provider(SimpleNamespace()), GoogleCalendar)


def test_switches_are_independent() -> None:
    s = SimpleNamespace(email_provider="microsoft", calendar_provider="google")
    assert isinstance(get_mail_provider(s), MicrosoftMail)
    assert isinstance(get_calendar_provider(s), GoogleCalendar)
    s = SimpleNamespace(email_provider="google", calendar_provider="microsoft")
    assert isinstance(get_mail_provider(s), GoogleMail)
    assert isinstance(get_calendar_provider(s), MicrosoftCalendar)


def test_unknown_value_on_a_stub_falls_back_to_google() -> None:
    assert isinstance(get_mail_provider(SimpleNamespace(email_provider="yahoo")), GoogleMail)


def test_stub_values_are_case_insensitive() -> None:
    assert isinstance(get_mail_provider(SimpleNamespace(email_provider=" MICROSOFT ")), MicrosoftMail)
    assert isinstance(get_calendar_provider(SimpleNamespace(calendar_provider="Microsoft")), MicrosoftCalendar)


def test_backends_satisfy_the_protocols() -> None:
    for mail in (GoogleMail(), MicrosoftMail()):
        assert isinstance(mail, MailProvider)
    for cal in (GoogleCalendar(), MicrosoftCalendar()):
        assert isinstance(cal, CalendarProvider)


def test_settings_reads_env_and_rejects_typos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "microsoft")
    monkeypatch.setenv("CALENDAR_PROVIDER", "microsoft")
    s = Settings()
    assert s.email_provider == "microsoft"
    assert s.calendar_provider == "microsoft"
    assert isinstance(get_mail_provider(s), MicrosoftMail)
    monkeypatch.setenv("EMAIL_PROVIDER", "outlook")
    with pytest.raises(ValueError):
        Settings()


def test_provider_server_missing(tmp_path: Path) -> None:
    config = tmp_path / "mcp_servers.json"
    assert provider_server_missing("microsoft_365", config) is False  # no config at all
    config.write_text(json.dumps({"mcpServers": {"google_workspace": {"command": "x"}}}))
    assert provider_server_missing("microsoft_365", config) is True
    assert provider_server_missing("google_workspace", config) is False


def test_log_provider_status_warns_on_missing_server(tmp_path: Path) -> None:
    """Attach a handler to the registry logger directly: the app's logging
    config (exercised elsewhere in a full run) sets ``propagate = False`` on
    the ``openexecutive`` tree, so ``caplog`` on the root logger misses it."""
    config = tmp_path / "mcp_servers.json"
    config.write_text(json.dumps({"mcpServers": {"google_workspace": {"command": "x"}}}))
    s = SimpleNamespace(email_provider="microsoft", calendar_provider="google")

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger("openexecutive.integrations.workspace.registry")
    handler = _Collect(level=logging.DEBUG)
    prior_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        log_provider_status(s, config)
    finally:
        target.removeHandler(handler)
        target.setLevel(prior_level)

    messages = [r.getMessage() for r in records]
    assert any(
        "email=microsoft (microsoft_365) calendar=google (google_workspace)" in m for m in messages
    )
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "EMAIL_PROVIDER=microsoft" in warnings[0].getMessage()
    assert "microsoft_365" in warnings[0].getMessage()


def test_log_provider_status_warns_on_a_misnamed_microsoft_server(tmp_path: Path) -> None:
    """The gate is keyed to the literal `microsoft_365` name; a look-alike
    under another name would expose ungated tools, so startup says so."""
    config = tmp_path / "mcp_servers.json"
    config.write_text(json.dumps({"mcpServers": {
        "google_workspace": {"command": "x"}, "outlook": {"command": "y"},
    }}))
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger("openexecutive.integrations.workspace.registry")
    handler = _Collect(level=logging.DEBUG)
    target.addHandler(handler)
    prior = target.level
    target.setLevel(logging.DEBUG)
    try:
        log_provider_status(SimpleNamespace(), config)
    finally:
        target.removeHandler(handler)
        target.setLevel(prior)
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert any("'outlook'" in m and "microsoft_365" in m for m in warnings)
