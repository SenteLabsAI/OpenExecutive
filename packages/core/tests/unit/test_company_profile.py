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
