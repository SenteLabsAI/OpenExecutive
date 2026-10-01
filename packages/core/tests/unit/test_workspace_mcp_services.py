"""Guards which Google services the co-located workspace-mcp child loads.

docker/workspace-mcp-launch.sh passes ``--tool-tier`` and ``--tools`` from
WORKSPACE_MCP_TOOL_TIER / WORKSPACE_MCP_TOOLS; the default is every service at
the complete tier. These tests run the real script against a stub binary and
read back the arguments it would start workspace-mcp with.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from openexecutive.orchestrator import mcp_gateway

REPO = Path(__file__).resolve().parents[4]
LAUNCHER = REPO / "docker" / "workspace-mcp-launch.sh"
DOCKERFILE = REPO / "docker" / "Dockerfile"
REAL_BINARY = "/opt/workspace-mcp/bin/workspace-mcp"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _launch_args(tmp_path: Path, **env: str) -> list[str]:
    stub = tmp_path / "workspace-mcp"
    stub.write_text(
        '#!/bin/sh\nprintf "env=%s\\n" "${WORKSPACE_MCP_TOOLS-unset}"\n'
        'for a in "$@"; do printf "%s\\n" "$a"; done\n'
    )
    stub.chmod(0o755)
    script = tmp_path / "launch.sh"
    script.write_text(LAUNCHER.read_text().replace(REAL_BINARY, str(stub)))
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = subprocess.run(
        ["sh", str(script)], env={**base, **env}, capture_output=True, text=True, check=True,
        cwd=tmp_path,
    )
    return result.stdout.splitlines()


def _services(args: list[str]) -> list[str] | None:
    if "--tools" not in args:
        return None
    rest = args[args.index("--tools") + 1 :]
    end = next((i for i, a in enumerate(rest) if a.startswith("--")), len(rest))
    return rest[:end]


def _tier(args: list[str]) -> str:
    return args[args.index("--tool-tier") + 1]


def test_default_loads_every_service_at_the_complete_tier(tmp_path: Path) -> None:
    args = _launch_args(tmp_path)
    assert _tier(args) == "complete"
    assert _services(args) is None
    assert "--single-user" in args


@pytest.mark.parametrize("value", ["$WORKSPACE_MCP_TOOLS", "", ",", " , ", "all"])
def test_no_service_names_means_every_service(tmp_path: Path, value: str) -> None:
    # extensible-mcp passes `$VAR` through literally when the API lacks VAR.
    args = _launch_args(
        tmp_path, WORKSPACE_MCP_TOOLS=value, WORKSPACE_MCP_TOOL_TIER="$WORKSPACE_MCP_TOOL_TIER"
    )
    assert _tier(args) == "complete"
    assert _services(args) is None


def test_services_and_tier_can_be_set(tmp_path: Path) -> None:
    args = _launch_args(tmp_path, WORKSPACE_MCP_TOOLS="gmail,calendar,drive", WORKSPACE_MCP_TOOL_TIER="extended")
    assert _tier(args) == "extended"
    assert _services(args) == ["gmail", "calendar", "drive"]


def test_service_names_are_never_glob_expanded(tmp_path: Path) -> None:
    (tmp_path / "gmail-backup").write_text("")
    args = _launch_args(tmp_path, WORKSPACE_MCP_TOOLS="gmail*,drive")
    assert _services(args) == ["gmail*", "drive"]


@pytest.mark.parametrize("value", ["all", "gmail,drive", "$WORKSPACE_MCP_TOOLS"])
def test_the_child_never_sees_the_service_variable(tmp_path: Path, value: str) -> None:
    # workspace-mcp falls back to reading WORKSPACE_MCP_TOOLS itself, and
    # rejects "all" as a service name.
    assert _launch_args(tmp_path, WORKSPACE_MCP_TOOLS=value)[0] == "env=unset"


def test_dockerfile_defaults_match_the_launcher() -> None:
    env = dict(re.findall(r"^ENV (WORKSPACE_MCP_TOOLS|WORKSPACE_MCP_TOOL_TIER)=(\S+)$", DOCKERFILE.read_text(), re.M))
    assert env == {"WORKSPACE_MCP_TOOL_TIER": "complete", "WORKSPACE_MCP_TOOLS": "all"}


def test_gateway_forwards_the_service_list() -> None:
    # Without it an operator's WORKSPACE_MCP_TOOLS never reaches the child.
    assert "WORKSPACE_MCP_TOOLS" in mcp_gateway._FORWARDED_ENV_VARS
