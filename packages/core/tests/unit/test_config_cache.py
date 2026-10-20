"""get_settings() builds Settings once and rebuilds only when its inputs change."""

from pathlib import Path

import pytest

from openexecutive import config


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_settings_cache", None)


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    builds = [0]
    real = config.Settings

    def counting(*args: object, **kwargs: object) -> config.Settings:
        builds[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(config, "Settings", counting)
    return builds


def test_repeat_calls_reuse_one_build(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = _count_builds(monkeypatch)
    first = config.get_settings()
    second = config.get_settings()
    assert builds[0] == 1
    assert first.model_dump() == second.model_dump()


def test_an_environment_change_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = _count_builds(monkeypatch)
    monkeypatch.setenv("HONCHO_WORKSPACE_ID", "first")
    assert config.get_settings().honcho_workspace_id == "first"
    monkeypatch.setenv("HONCHO_WORKSPACE_ID", "second")
    assert config.get_settings().honcho_workspace_id == "second"
    monkeypatch.delenv("HONCHO_WORKSPACE_ID")
    assert config.get_settings().honcho_workspace_id == "openexec"
    assert builds[0] == 3


def test_an_env_file_change_rebuilds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HONCHO_WORKSPACE_ID=from-file\n")
    monkeypatch.delenv("HONCHO_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(config, "_ENV_FILE", env_file)
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(env_file))
    assert config.get_settings().honcho_workspace_id == "from-file"
    env_file.write_text("HONCHO_WORKSPACE_ID=changed-in-file\n")
    assert config.get_settings().honcho_workspace_id == "changed-in-file"


def test_the_mcp_config_file_appearing_rebuilds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mcp_file = tmp_path / "mcp_servers.json"
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    monkeypatch.setenv("MCP_SERVERS_CONFIG_PATH", str(mcp_file))
    assert config.get_settings().mcp_enabled is False
    mcp_file.write_text("{}")
    assert config.get_settings().mcp_enabled is True


def test_a_caller_changing_its_copy_changes_no_one_else() -> None:
    mine = config.get_settings()
    mine.honcho_workspace_id = "changed"
    mine.local_models.append("leaked")
    theirs = config.get_settings()
    assert theirs.honcho_workspace_id != "changed"
    assert "leaked" not in theirs.local_models
