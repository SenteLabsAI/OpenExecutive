"""Tests for scripts/pr_checks.py, the CI gate for the PR rules in CLAUDE.md."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "pr_checks.py"
_spec = importlib.util.spec_from_file_location("pr_checks", _SCRIPT)
assert _spec and _spec.loader
pr_checks = importlib.util.module_from_spec(_spec)
sys.modules["pr_checks"] = pr_checks  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(pr_checks)

Change = pr_checks.Change
PKG = "packages/core/openexecutive/"
PREBUILT = PKG + "architecture/prebuilt/"


def _level(check, **kw) -> str:
    kw.setdefault("changed", set())
    kw["changed"] = set(kw["changed"]) | set(kw.get("added", ()))
    return check(Change(**kw)).level


# --- arch-doc-drift ----------------------------------------------------------


def test_drift_fails_when_documented_module_changes_without_its_section() -> None:
    changed = {PKG + "integrations/slack.py", PREBUILT + "scheduler.json"}
    assert _level(pr_checks.check_arch_drift, changed=changed) == "FAIL"


def test_drift_passes_when_matching_section_edited() -> None:
    changed = {PKG + "integrations/slack.py", PREBUILT + "integrations.json"}
    assert _level(pr_checks.check_arch_drift, changed=changed) == "PASS"


def test_drift_accepts_any_of_several_sections() -> None:
    changed = {PKG + "orchestrator/executive.py", PREBUILT + "lifecycle.json"}
    assert _level(pr_checks.check_arch_drift, changed=changed) == "PASS"


def test_drift_yaml_only_edit_does_not_count() -> None:
    changed = {PKG + "scheduler/jobs.py", PKG + "architecture/architecture-facts.yaml"}
    assert _level(pr_checks.check_arch_drift, changed=changed) == "FAIL"


def test_drift_ignores_non_doc_modules_and_other_packages() -> None:
    changed = {
        PKG + "utils/text.py",
        PKG + "cli.py",
        PKG + "evals/runner.py",
        "packages/ui/src/app/page.tsx",
        "packages/core/tests/unit/test_x.py",
    }
    assert _level(pr_checks.check_arch_drift, changed=changed) == "PASS"


def test_drift_unmapped_module_needs_some_section() -> None:
    assert _level(pr_checks.check_arch_drift, changed={PKG + "config.py"}) == "FAIL"
    changed = {PKG + "config.py", PREBUILT + "overview.json"}
    assert _level(pr_checks.check_arch_drift, changed=changed) == "PASS"


def test_drift_new_module_needs_section_spec_page_and_json() -> None:
    added = {PKG + "billing/__init__.py", PKG + "billing/core.py"}
    result = pr_checks.check_arch_drift(Change(changed=set(added), added=added))
    assert result.level == "FAIL"
    assert "billing.json" in result.detail and "sections.py" in result.detail

    changed = added | {
        PREBUILT + "billing.json",
        PKG + "architecture/sections.py",
        "packages/ui/src/app/architecture/page.tsx",
    }
    assert _level(pr_checks.check_arch_drift, changed=changed, added=added) == "PASS"


@pytest.mark.parametrize(
    "text",
    ["Arch-Docs: n/a - internal rename", "fix x\n\narch-docs: N/A — no behavior change"],
)
def test_drift_waiver_line_passes(text: str) -> None:
    changed = {PKG + "integrations/slack.py"}
    assert _level(pr_checks.check_arch_drift, changed=changed, waiver_text=text) == "PASS"


@pytest.mark.parametrize(
    "text", ["we did not add Arch-Docs: n/a - here", "Arch-Docs: n/a", "Arch-Docs: na -"]
)
def test_drift_waiver_needs_own_line_and_reason(text: str) -> None:
    changed = {PKG + "integrations/slack.py"}
    assert _level(pr_checks.check_arch_drift, changed=changed, waiver_text=text) == "FAIL"


# --- no-stubs ----------------------------------------------------------------


@pytest.mark.parametrize(
    "line", ["    # TODO: finish", "    raise NotImplementedError", "    pass  # stub"]
)
def test_no_stubs_fails_on_stub_in_code(line: str) -> None:
    lines = {PKG + "memory/store.py": ["x = 1", line]}
    assert _level(pr_checks.check_no_stubs, added_lines=lines) == "FAIL"


def test_no_stubs_ignores_docs_and_exempt_files() -> None:
    lines = {
        "docs/roadmap.md": ["- TODO: write this"],
        "scripts/pr_checks.py": ["STUB_RE = 'TODO'"],
        PKG + "memory/store.py": ["todos = load_todos()"],
    }
    assert _level(pr_checks.check_no_stubs, added_lines=lines) == "PASS"


# --- eval-scenarios ----------------------------------------------------------


def test_eval_scenarios_required_for_new_agent() -> None:
    added = {PKG + "agents/sales.py"}
    assert _level(pr_checks.check_eval_scenarios, added=added) == "FAIL"
    changed = {PKG + "evals/_scenarios/sales_001.yaml"}
    assert _level(pr_checks.check_eval_scenarios, changed=changed, added=added) == "PASS"


def test_eval_scenarios_required_for_domain_prompt_change() -> None:
    changed = {PKG + "prompts/domain_prompts.py"}
    assert _level(pr_checks.check_eval_scenarios, changed=changed) == "FAIL"


def test_eval_scenarios_not_required_for_editing_existing_agent() -> None:
    changed = {PKG + "agents/finance.py"}
    assert _level(pr_checks.check_eval_scenarios, changed=changed) == "PASS"


def test_eval_scenarios_not_required_for_rename_or_private_module() -> None:
    renamed = {PKG + "agents/finance_agent.py"}
    assert _level(pr_checks.check_eval_scenarios, changed=renamed, renamed=renamed) == "PASS"
    added = {PKG + "agents/_helpers.py", PKG + "agents/__init__.py"}
    assert _level(pr_checks.check_eval_scenarios, added=added) == "PASS"


def test_parse_added_lines_only_trusts_headers() -> None:
    diff = "\n".join(
        [
            "diff --git a/x.py b/x.py",
            "--- a/x.py",
            "+++ b/x.py",
            "@@ -1 +1,3 @@",
            "+++ a",  # an added line that reads "++ a"
            "++++ b/other.py",
            "+y = 2  # TODO",
            'diff --git "a/t\\tab.py" "b/t\\tab.py"',
            "--- /dev/null",
            '+++ "b/t\\tab.py"',
            "@@ -0,0 +1 @@",
            "+z = 1",
            "diff --git a/gone.py b/gone.py",
            "--- a/gone.py",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-old",
        ]
    )
    assert pr_checks._parse_added_lines(diff) == {
        "x.py": ["++ a", "+++ b/other.py", "y = 2  # TODO"],
        "t\tab.py": ["z = 1"],
    }


# --- tests-present -----------------------------------------------------------


def test_tests_present_warns_only() -> None:
    changed = {PKG + "memory/store.py"}
    assert _level(pr_checks.check_tests_present, changed=changed) == "WARN"
    changed.add("packages/core/tests/unit/test_store.py")
    assert _level(pr_checks.check_tests_present, changed=changed) == "PASS"


# --- git plumbing ------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_collect_and_main_against_a_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    slack = repo / PKG / "integrations" / "slack.py"
    slack.parent.mkdir(parents=True)
    slack.write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feature")

    slack.write_text("x = 1\ny = 2  # TODO\n")  # uncommitted edit
    new = repo / PKG / "agents" / "sales.py"
    new.parent.mkdir(parents=True)
    new.write_text("z = 3\n")  # untracked file
    monkeypatch.chdir(repo)
    monkeypatch.delenv("PR_BODY", raising=False)

    change = pr_checks.collect("main")
    assert PKG + "integrations/slack.py" in change.changed
    assert PKG + "agents/sales.py" in change.added
    assert change.added_lines[PKG + "integrations/slack.py"] == ["y = 2  # TODO"]

    assert pr_checks.main(["--base", "main"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  arch-doc-drift" in out and "FAIL  no-stubs" in out

    slack.write_text("x = 1\ny = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add sales\n\nArch-Docs: n/a - test fixture")
    monkeypatch.setenv("PR_BODY", "")
    assert pr_checks.main(["--base", "main"]) == 1  # eval scenarios still missing
    out = capsys.readouterr().out
    assert "waived (Arch-Docs: n/a - test fixture)" in out and "FAIL  eval-scenarios" in out

    monkeypatch.setenv("PR_BODY", "## Problem\n\nArch-Docs: n/a - from the PR body\n")
    _git(repo, "commit", "-q", "--amend", "-m", "add sales")
    pr_checks.main(["--base", "main"])
    assert "waived (Arch-Docs: n/a - from the PR body)" in capsys.readouterr().out


def test_collect_survives_odd_files_and_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "diff.noprefix", "true")
    _git(repo, "config", "diff.renames", "false")
    old = repo / PKG / "memory" / "old name.py"
    old.parent.mkdir(parents=True)
    old.write_text("# TODO carried over\n" * 5)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-qb", "feature")
    _git(repo, "mv", str(old), str(old.with_name("new name.py")))
    integ = repo / PKG / "integrations"
    integ.mkdir(parents=True)
    (integ / "caf\u00e9.py").write_text("# TODO\n")
    (integ / "latin1.txt").write_bytes(b"caf\xe9\n")
    (integ / "sp ace.py").write_text("raise NotImplementedError\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "odd files")
    monkeypatch.chdir(repo)

    change = pr_checks.collect("main")
    assert PKG + "integrations/caf\u00e9.py" in change.added
    assert change.added_lines[PKG + "integrations/caf\u00e9.py"] == ["# TODO"]
    assert change.added_lines[PKG + "integrations/sp ace.py"] == ["raise NotImplementedError"]
    assert PKG + "integrations/latin1.txt" in change.changed
    assert {PKG + "memory/old name.py", PKG + "memory/new name.py"} <= change.changed
    assert change.renamed == {PKG + "memory/new name.py"}
    assert PKG + "memory/new name.py" not in change.added_lines  # a pure move adds no lines
    assert pr_checks.check_no_stubs(change).level == "FAIL"
    assert pr_checks.check_arch_drift(change).level == "FAIL"


def test_main_reports_missing_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "base")
    monkeypatch.chdir(tmp_path)
    assert pr_checks.main(["--base", "origin/nope"]) == 2
    assert "is 'origin/nope' fetched?" in capsys.readouterr().out
