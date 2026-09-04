"""Unit tests for CompanyProfile."""
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


def test_save_to_yaml_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "nested" / "profile.yaml"
    CompanyProfile(name="First").save_to_yaml(path)
    CompanyProfile(name="Second").save_to_yaml(path)
    assert CompanyProfile.load_from_yaml(path).name == "Second"
    assert [p.name for p in path.parent.iterdir()] == ["profile.yaml"]
