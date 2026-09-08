"""Unit tests for CompanyProfile."""
import builtins
import tempfile
from pathlib import Path

from openexecutive.memory.company_profile import CompanyProfile


def test_empty_profile():
    profile = CompanyProfile()
    assert profile.is_empty()
    assert profile.to_prompt_block() == ""


def test_profile_with_name():
    profile = CompanyProfile(name="Acme Corp", industry="B2B SaaS", stage="Series A")
    assert not profile.is_empty()
    block = profile.to_prompt_block()
    assert "Acme Corp" in block
    assert "B2B SaaS" in block
    assert "Series A" in block


def test_profile_yaml_roundtrip():
    profile = CompanyProfile(
        name="TestCo",
        industry="Fintech",
        stage="Seed",
        headcount=12,
        annual_revenue_arr=500000.0,
    )

    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        path = Path(f.name)

    try:
        profile.save_to_yaml(path)
        loaded = CompanyProfile.load_from_yaml(path)

        assert loaded.name == "TestCo"
        assert loaded.industry == "Fintech"
        assert loaded.headcount == 12
        assert loaded.annual_revenue_arr == 500000.0
    finally:
        path.unlink(missing_ok=True)


def test_profile_yaml_io_pins_utf8(tmp_path: Path, monkeypatch):
    """profile.yaml must be read and written as UTF-8, not the platform default.

    save_to_yaml goes through yaml.dump with allow_unicode=False, so anything it
    writes is pure ASCII. But profile.yaml is routinely hand-edited, and such a
    file is UTF-8 — read at the platform default (cp1252 on Windows) a single
    em dash came back as three characters, silently corrupting the cached
    company-profile prompt block instead of raising. Worse,
    _restore_slot_state re-saved the mojibake, persisting it to the live file.

    Asserts the contract (both sites pass an explicit encoding) rather than the
    symptom: the locale cannot be faked from Python, so a symptom test would be
    inert on the UTF-8 platform CI runs on. The value assertions below then
    confirm the content actually round-trips.
    """
    opened: list[str | None] = []
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if "b" not in mode and str(file).endswith("profile.yaml"):
            opened.append(kwargs.get("encoding"))
            kwargs.setdefault("encoding", "utf-8")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    path = tmp_path / "profile.yaml"
    path.write_text(
        "company:\n"
        "  name: Consultório Ação\n"
        "  stage: Pré-operacional — em fase de abertura\n"
        "  mission: Orçamento ≈ R$ 300 → presença digital\n",
        encoding="utf-8",
    )

    loaded = CompanyProfile.load_from_yaml(path)
    loaded.save_to_yaml(tmp_path / "out" / "profile.yaml")
    monkeypatch.undo()

    # Both the read and the write passed an explicit encoding.
    assert opened == ["utf-8", "utf-8"]

    assert loaded.name == "Consultório Ação"
    assert loaded.stage == "Pré-operacional — em fase de abertura"
    assert loaded.mission == "Orçamento ≈ R$ 300 → presença digital"
    # The em dash must survive as one character, not three mojibake bytes.
    assert loaded.stage.count("—") == 1
    assert "â" not in loaded.stage


def test_prompt_block_includes_financials():
    from openexecutive.memory.company_profile import Financials

    profile = CompanyProfile(
        name="BurnCo",
        industry="SaaS",
        stage="Series A",
        financials=Financials(burn_rate_monthly=300000.0, runway_months=8.0),
    )
    block = profile.to_prompt_block()
    assert "300,000" in block
    assert "8.0 months" in block


def test_save_to_yaml_is_atomic(tmp_path: Path, monkeypatch):
    """A failure mid-dump must leave the previous profile.yaml untouched.

    The onboarding route rolls its session back on a build failure on the
    assumption that the on-disk profile every other subsystem loads survived.
    A plain open(path, "w") truncates before yaml.dump writes a byte.
    """
    import yaml

    path = tmp_path / "profile.yaml"
    CompanyProfile(name="Before").save_to_yaml(path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(yaml, "dump", boom)
    try:
        CompanyProfile(name="After").save_to_yaml(path)
    except OSError:
        pass
    else:  # pragma: no cover - the monkeypatch must propagate
        raise AssertionError("save_to_yaml swallowed the failure")
    monkeypatch.undo()

    assert CompanyProfile.load_from_yaml(path).name == "Before"
    assert [p.name for p in tmp_path.iterdir()] == ["profile.yaml"], "temp file left behind"


def test_save_to_yaml_preserves_mode(tmp_path: Path):
    """Rename creates a new inode; an operator's chmod must survive it.

    0o664 rather than 0o600 on purpose: the temp file is always created
    0o600, so a mode equal to the default would pass even if the fchmod
    that carries the destination's mode across were removed.
    """
    import os
    import stat

    path = tmp_path / "profile.yaml"
    CompanyProfile(name="Before").save_to_yaml(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "fresh profile should be private"
    os.chmod(path, 0o664)

    CompanyProfile(name="After").save_to_yaml(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    assert CompanyProfile.load_from_yaml(path).name == "After"
    assert [p.name for p in tmp_path.iterdir()] == ["profile.yaml"]


def test_save_to_yaml_through_symlink_keeps_link_and_target_mode(tmp_path: Path):
    """A symlinked profile path writes the target and never uses the link's 0777.

    The target is chmod'd 0o640 (not the 0o600 default) so the test also
    proves the mode carried across came from the target, not the link.
    """
    import os
    import stat

    target = tmp_path / "volume" / "profile.yaml"
    target.parent.mkdir()
    CompanyProfile(name="Before").save_to_yaml(target)
    os.chmod(target, 0o640)
    link = tmp_path / "profile.yaml"
    link.symlink_to(target)

    CompanyProfile(name="After").save_to_yaml(link)

    assert link.is_symlink(), "the symlink must survive the save"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert CompanyProfile.load_from_yaml(link).name == "After"
    assert [p.name for p in target.parent.iterdir()] == ["profile.yaml"]


def test_save_to_yaml_replaces_a_symlink_loop(tmp_path: Path):
    """A looped symlink at the profile path is broken config, not a crash.

    Path.resolve() raises RuntimeError on 3.11 for a loop; save_to_yaml must
    fall back rather than let that escape (the profile-editor route has no
    handler for it). The loop is replaced by a private regular file.
    """
    import stat

    link = tmp_path / "profile.yaml"
    link.symlink_to(link)

    CompanyProfile(name="X").save_to_yaml(link)

    assert not link.is_symlink()
    assert stat.S_IMODE(link.stat().st_mode) == 0o600
    assert CompanyProfile.load_from_yaml(link).name == "X"
    assert [p.name for p in tmp_path.iterdir()] == ["profile.yaml"]


def test_concurrent_saves_do_not_break_each_other(tmp_path: Path):
    """Two writers on one path must both succeed (last writer wins)."""
    import threading

    path = tmp_path / "profile.yaml"
    errors: list[BaseException] = []

    def _worker(name: str) -> None:
        try:
            for _ in range(40):
                CompanyProfile(name=name).save_to_yaml(path)
        except BaseException as exc:  # noqa: BLE001 - collecting for the assert
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert CompanyProfile.load_from_yaml(path).name.startswith("w")
    assert [p.name for p in tmp_path.iterdir()] == ["profile.yaml"]
