"""docker/ms365-mcp-launch.sh — the co-located Microsoft 365 MCP child's launcher.

Run with `sh` against a stub server binary (MS365_MCP_SERVER_BIN) that echoes
its argv and environment, so the contract the gateway and the docs rely on is
pinned: the `$VAR` placeholder scrub, the required client id, the token-cache
paths on the credentials dir, keytar off, the preset, and pass-through of the
seeding flags.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = REPO_ROOT / "docker" / "ms365-mcp-launch.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")

_STUB = """#!/bin/sh
printf 'ARGV:'
for a in "$@"; do printf ' [%s]' "$a"; done
printf '\\n'
env | grep '^MS365_MCP_' | sort
"""


def _run(tmp_path: Path, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "stub-server.sh"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MS365_MCP_SERVER_BIN": str(stub),
        "MS365_MCP_CREDENTIALS_DIR": str(tmp_path / "creds"),
    }
    base.update(env)
    return subprocess.run(
        ["sh", str(LAUNCHER), *extra], env=base, capture_output=True, text=True, timeout=30,
    )


def test_missing_client_id_exits_64(tmp_path: Path) -> None:
    proc = _run(tmp_path, {})
    assert proc.returncode == 64
    assert "MS365_MCP_CLIENT_ID" in proc.stderr


def test_literal_placeholder_client_id_is_treated_as_unset(tmp_path: Path) -> None:
    """extensible-mcp leaves an unset `$VAR` as that literal string."""
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "$MS365_MCP_CLIENT_ID"})
    assert proc.returncode == 64


def test_literal_placeholder_optional_vars_are_scrubbed(tmp_path: Path) -> None:
    proc = _run(tmp_path, {
        "MS365_MCP_CLIENT_ID": "client-1",
        "MS365_MCP_TENANT_ID": "$MS365_MCP_TENANT_ID",
        "MS365_MCP_EXPECTED_USERNAME": "$MS365_MCP_EXPECTED_USERNAME",
        "MS365_MCP_CLIENT_SECRET": "$MS365_MCP_CLIENT_SECRET",
    })
    assert proc.returncode == 0, proc.stderr
    assert "MS365_MCP_TENANT_ID=" not in proc.stdout
    assert "MS365_MCP_CLIENT_SECRET=" not in proc.stdout
    assert "--expected-username" not in proc.stdout


def _argv(proc: subprocess.CompletedProcess[str]) -> list[str]:
    line = proc.stdout.splitlines()[0]
    assert line.startswith("ARGV:")
    return re.findall(r"\[([^\]]*)\]", line)


# Every tool the launcher's default allow-list admits. Each one is either
# roster-gated in orchestrator/mcp_gateway.py or carries no recipient.
_EXPECTED_ALLOWED = {
    "list-mail-folders", "list-mail-folder-messages", "list-mail-messages", "get-mail-message",
    "list-mail-attachments", "download-bytes", "send-mail", "reply-mail-message",
    "reply-all-mail-message", "forward-mail-message", "create-draft-email", "create-reply-draft",
    "create-reply-all-draft", "create-forward-draft", "update-mail-message", "send-draft-message",
    "move-mail-message", "list-calendars", "list-calendar-events", "get-calendar-event",
    "create-calendar-event", "update-calendar-event", "delete-calendar-event",
    "cancel-calendar-event", "accept-calendar-event", "decline-calendar-event",
    "tentatively-accept-calendar-event", "get-calendar-view",
}
# Tools the preset would have registered that must NOT be admitted: they can
# address people the gateway never sees (forwarding rules, external auto-reply,
# calendar sharing) or write to the container filesystem.
_MUST_BE_EXCLUDED = {
    "create-mail-rule", "update-mail-rule", "update-mailbox-settings", "forward-calendar-event",
    "create-specific-calendar-event", "create-my-calendar-permission", "delete-calendar",
    "delete-mail-message", "download-bytes-to-file", "get-mail-message-mime",
}


def test_default_argv_is_an_explicit_anchored_allow_list(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "client-1"})
    assert proc.returncode == 0, proc.stderr
    argv = _argv(proc)
    assert argv[0] == "--enabled-tools"
    pattern = argv[1]
    assert pattern.startswith("^(") and pattern.endswith(")$")
    regex = re.compile(pattern)
    assert set(pattern[2:-2].split("|")) == _EXPECTED_ALLOWED
    for name in _EXPECTED_ALLOWED:
        assert regex.match(name), name
    for name in _MUST_BE_EXCLUDED:
        assert not regex.match(name), name
    assert "--preset" not in argv


def test_seeding_flags_pass_through_after_the_allow_list(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "client-1"}, "--login")
    assert proc.returncode == 0, proc.stderr
    argv = _argv(proc)
    assert argv[0] == "--enabled-tools" and argv[-1] == "--login"


def test_preset_and_enabled_tools_overrides(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "client-1", "MS365_MCP_PRESET": "mail,calendar"})
    assert proc.returncode == 0, proc.stderr
    assert _argv(proc)[:2] == ["--preset", "mail,calendar"]
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "client-1", "MS365_MCP_ENABLED_TOOLS": "^(send-mail)$"})
    assert _argv(proc)[:2] == ["--enabled-tools", "^(send-mail)$"]
    # A literal placeholder for either override is scrubbed back to the default.
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "client-1", "MS365_MCP_PRESET": "$MS365_MCP_PRESET"})
    assert _argv(proc)[0] == "--enabled-tools"


def test_expected_username_becomes_a_flag(tmp_path: Path) -> None:
    proc = _run(
        tmp_path,
        {"MS365_MCP_CLIENT_ID": "client-1", "MS365_MCP_EXPECTED_USERNAME": "exec@contoso.com"},
        "--verify-login",
    )
    assert proc.returncode == 0, proc.stderr
    argv = _argv(proc)
    assert argv[0] == "--enabled-tools"
    assert argv[2:] == ["--expected-username", "exec@contoso.com", "--verify-login"]
    assert "MS365_MCP_EXPECTED_USERNAME=exec@contoso.com" in proc.stdout


def test_token_paths_default_under_credentials_dir_and_dir_is_private(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"MS365_MCP_CLIENT_ID": "client-1"})
    assert proc.returncode == 0, proc.stderr
    creds = tmp_path / "creds"
    assert creds.is_dir()
    assert stat.S_IMODE(creds.stat().st_mode) == 0o700
    assert f"MS365_MCP_TOKEN_CACHE_PATH={creds}/.token-cache.json" in proc.stdout
    assert f"MS365_MCP_SELECTED_ACCOUNT_PATH={creds}/.selected-account.json" in proc.stdout
    assert "MS365_MCP_USE_KEYTAR=0" in proc.stdout


def test_explicit_token_path_and_keytar_are_respected(tmp_path: Path) -> None:
    proc = _run(tmp_path, {
        "MS365_MCP_CLIENT_ID": "client-1",
        "MS365_MCP_TOKEN_CACHE_PATH": "/elsewhere/cache.json",
        "MS365_MCP_USE_KEYTAR": "false",
    })
    assert proc.returncode == 0, proc.stderr
    assert "MS365_MCP_TOKEN_CACHE_PATH=/elsewhere/cache.json" in proc.stdout
    assert "MS365_MCP_USE_KEYTAR=false" in proc.stdout


def test_launcher_is_executable_in_git() -> None:
    assert LAUNCHER.exists()
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR
