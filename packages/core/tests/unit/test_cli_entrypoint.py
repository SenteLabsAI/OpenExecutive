"""The `openexecutive` console script must resolve to the click group.

Regression guard: a `cli.py` module next to the `cli/` package was shadowed by
the package, so `openexecutive.cli:cli` (pyproject `[project.scripts]`) failed.
"""
from __future__ import annotations

import click
from click.testing import CliRunner

from openexecutive.cli import cli


def test_console_script_target_is_click_group() -> None:
    assert isinstance(cli, click.Group)


def test_help_lists_all_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    for name in ("ask", "chat", "ingest-oer", "consolidate-initiatives", "onboard", "seed-org"):
        assert name in result.output, f"missing command {name!r} in --help output"
