"""Tests for scripts/changed_tests.py, which picks the tests to run for a branch."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "changed_tests.py"
_spec = importlib.util.spec_from_file_location("changed_tests", _SCRIPT)
assert _spec and _spec.loader
changed_tests = importlib.util.module_from_spec(_spec)
sys.modules["changed_tests"] = changed_tests
_spec.loader.exec_module(changed_tests)

PKG = "packages/core/openexecutive/"
TESTS = "packages/core/tests/"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny repo layout: two unit tests and one integration test."""
    core = tmp_path / "packages" / "core"
    for rel, text in {
        "tests/unit/test_episodic.py": "from openexecutive.memory.episodic import x\n",
        "tests/unit/test_slack.py": "import openexecutive.integrations.slack\n",
        "tests/integration/test_chat.py": "from openexecutive.memory import episodic\n"
        "# openexecutive.memory\n",
    }.items():
        path = core / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(changed_tests, "ROOT", tmp_path)
    monkeypatch.setattr(changed_tests, "CORE", core)
    return tmp_path


def test_dotted_module_path() -> None:
    assert changed_tests.dotted(PKG + "memory/episodic.py") == "openexecutive.memory.episodic"


def test_dotted_package_init_is_the_package() -> None:
    assert changed_tests.dotted(PKG + "memory/__init__.py") == "openexecutive.memory"


def test_source_change_selects_tests_that_mention_the_module(repo: Path) -> None:
    picked = changed_tests.select([PKG + "memory/episodic.py"])
    assert picked == ["tests/unit/test_episodic.py"]


def test_package_init_change_selects_every_test_under_that_package(repo: Path) -> None:
    picked = changed_tests.select([PKG + "memory/__init__.py"])
    assert picked == [
        "tests/integration/test_chat.py",
        "tests/unit/test_episodic.py",
    ]


def test_changed_test_file_is_selected_itself(repo: Path) -> None:
    picked = changed_tests.select([TESTS + "unit/test_slack.py"])
    assert picked == ["tests/unit/test_slack.py"]


def test_deleted_test_file_is_not_selected(repo: Path) -> None:
    assert changed_tests.select([TESTS + "unit/test_gone.py"]) == []


def test_unrelated_change_selects_nothing(repo: Path) -> None:
    assert changed_tests.select(["README.md", "packages/ui/src/app/page.tsx"]) == []


@pytest.mark.parametrize(
    "shared",
    [
        "packages/core/tests/conftest.py",
        "packages/core/pyproject.toml",
        "packages/core/uv.lock",
    ],
)
def test_shared_config_change_selects_the_whole_suite(repo: Path, shared: str) -> None:
    assert changed_tests.select([shared]) == ["tests/unit", "tests/integration"]
