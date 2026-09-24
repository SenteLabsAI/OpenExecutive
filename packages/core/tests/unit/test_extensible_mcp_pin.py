"""Guards the extensible-mcp pin shared by the MCP gateway and the API image.

The gateway launches extensible-mcp with ``uvx --exclude-newer <cutoff> --from
git+...@<commit>`` and the Dockerfile pre-warms uv's cache with the same
invocation. The commit pins extensible-mcp's code; the cutoff pins the
resolution of its dependencies, which uvx otherwise redoes against PyPI on
every networked start. Both invocations must match exactly: a mismatch means
the image caches one set of packages and the runtime resolves another, which is
the floating-code problem the pin exists to remove, plus a startup that needs
the network.
"""

from __future__ import annotations

import re
from pathlib import Path

from openexecutive.orchestrator import mcp_gateway

DOCKERFILE = Path(__file__).resolve().parents[4] / "docker" / "Dockerfile"
PREWARM = re.compile(r"\buvx\s+((?:\S+\s+)*?extensible-mcp)\b")
COMMIT_PIN = re.compile(r"^git\+https://github\.com/SenteLabsAI/extensible-mcp@[0-9a-f]{40}$")


def _dockerfile_prewarm_args() -> list[tuple[str, ...]]:
    """Arguments between ``uvx`` and ``extensible-mcp`` in every RUN that pre-warms it."""
    lines = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    joined = "\n".join(lines).replace("\\\n", " ")
    return [
        tuple(match.split())
        for instruction in joined.splitlines()
        if instruction.strip().startswith("RUN ")
        for match in PREWARM.findall(instruction)
    ]


def test_gateway_pins_extensible_mcp_to_a_full_commit() -> None:
    assert COMMIT_PIN.match(mcp_gateway._EXTENSIBLE_MCP_GIT), (
        f"{mcp_gateway._EXTENSIBLE_MCP_GIT!r} is not pinned to a 40-character commit; a branch, "
        "tag or short hash lets the gateway's code change without a new image"
    )
    assert mcp_gateway._EXTENSIBLE_MCP_GIT.endswith(mcp_gateway._EXTENSIBLE_MCP_REV)


def test_gateway_freezes_dependency_resolution_with_a_cutoff() -> None:
    args = mcp_gateway._EXTENSIBLE_MCP_LAUNCH_ARGS
    assert "--exclude-newer" in args, (
        "without --exclude-newer uvx re-resolves extensible-mcp's dependencies against PyPI on "
        "every networked start, so a pinned commit still runs new third-party code"
    )
    cutoff = args[args.index("--exclude-newer") + 1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cutoff), cutoff
    assert args[-3:] == ("--from", mcp_gateway._EXTENSIBLE_MCP_GIT, mcp_gateway._EXTENSIBLE_MCP_CMD)


def test_dockerfile_prewarm_matches_the_gateway_launch() -> None:
    prewarms = _dockerfile_prewarm_args()
    assert prewarms, "docker/Dockerfile no longer pre-warms extensible-mcp with uvx"
    assert set(prewarms) == {mcp_gateway._EXTENSIBLE_MCP_LAUNCH_ARGS}, (
        f"docker/Dockerfile pre-warms with {prewarms} but the gateway launches with "
        f"{mcp_gateway._EXTENSIBLE_MCP_LAUNCH_ARGS}; the image would cache one set of packages "
        "and resolve another at every start"
    )
