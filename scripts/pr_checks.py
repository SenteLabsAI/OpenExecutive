#!/usr/bin/env python3
"""Repo rules a PR must meet, checked against the diff from a base ref.

Runs in CI on every pull request and locally via `make check`:

    python3 scripts/pr_checks.py                 # diff vs origin/main
    python3 scripts/pr_checks.py --base origin/x

Checks (see CLAUDE.md -> "Architecture Docs" and "PR Requirements"):

- arch-doc-drift  FAIL  a documented module changed but its
                        architecture/prebuilt/<section>.json did not
- no-stubs        FAIL  added code lines carry TODO/FIXME/NotImplementedError
- eval-scenarios  FAIL  a new agent or a domain-prompt change without an
                        evals/_scenarios/ change
- tests-present   WARN  openexecutive/ code changed with no tests/ change

A release-please version bump is not a code change: when a file release-please
manages only has the version number rewritten on its marked lines (the release
PR's bump), it counts for neither arch-doc-drift nor tests-present.

A change that does not alter what a section describes can waive the drift
check with a line in a commit message or the PR description:

    Arch-Docs: n/a - <reason>

Stdlib only, so it runs without the project's virtualenv.
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

PKG = "packages/core/openexecutive/"
PREBUILT = PKG + "architecture/prebuilt/"
SECTIONS_PY = PKG + "architecture/sections.py"
ARCH_PAGE = "packages/ui/src/app/architecture/page.tsx"
TESTS = "packages/core/tests/"
SCENARIOS = PKG + "evals/_scenarios/"
AGENTS = PKG + "agents/"
DOMAIN_PROMPTS = PKG + "prompts/domain_prompts.py"

# Modules with no /architecture section.
NON_DOC = {"architecture", "utils", "cli", "evals", "__init__"}

# Module -> the section ids documenting it; editing any one satisfies the gate.
# A module missing here accepts an edit to any section. Keep in step with
# architecture/sections.py and the topic map in CLAUDE.md.
SECTIONS_FOR: dict[str, set[str]] = {
    "integrations": {"integrations"},
    "scheduler": {"scheduler"},
    "workflows": {"workflows"},
    "prompts": {"caching"},
    "departments": {"org"},
    "people": {"org"},
    "personas": {"org"},
    "onboarding": {"org"},
    "memory": {"memory", "peer_memory"},
    "api": {"api", "today"},
    "orchestrator": {"agents", "lifecycle", "review"},
    "agents": {"agents", "lifecycle", "review"},
    "providers": {"agents", "lifecycle", "review"},
    "audit": {"audit"},
    "attunement": {"attunement"},
    "monitoring": {"external_monitoring"},
    "mcp_server": {"mcp_server"},
    "clients": {"clients", "peer_memory"},
    "fixtures": {"clients", "peer_memory"},
    "knowledge": {"rag"},
    "briefing": {"today"},
    "alerts": {"today"},
    "guide": {"user_guide"},
}

# Needs a reason after the n/a: "Arch-Docs: n/a - renamed a helper".
WAIVER_RE = re.compile(
    r"^[ \t]*Arch-Docs:[ \t]*n/?a\b[ \t]*[-\u2013\u2014:][ \t]*\S.*$",
    re.IGNORECASE | re.MULTILINE,
)
STUB_RE = re.compile(
    r"\bTODO\b|\bFIXME\b|raise NotImplementedError|pass\s+#\s*stub|\.\.\.\s*#\s*stub"
)
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx")
# release-please rewrites the version on lines ending in this marker in its
# "generic" extra-files. Keep in step with release-please-config.json (a test
# checks); any other file carrying the marker gets no exemption.
VERSION_MARKER = "x-release-please-version"
RELEASE_PLEASE_FILES = {PKG + "api/main.py", PKG + "api/models.py"}
SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
# These files name the stub patterns themselves.
STUB_EXEMPT = {"scripts/pr_checks.py", TESTS + "unit/test_pr_checks.py"}


@dataclass
class Change:
    """The part of a diff the checks look at."""

    changed: set[str]
    added: set[str] = field(default_factory=set)
    # new paths of moved files: new to a module, but not new code
    renamed: set[str] = field(default_factory=set)
    # path -> lines the diff adds to that file
    added_lines: dict[str, list[str]] = field(default_factory=dict)
    # path -> lines the diff removes from that file
    removed_lines: dict[str, list[str]] = field(default_factory=dict)
    waiver_text: str = ""

    def version_bump_only(self, path: str) -> bool:
        """True when the diff to path only rewrites marked version numbers.

        Each removed line must match its added line once the version is
        masked, so other edits on a marked line still count as code.
        """
        if path not in RELEASE_PLEASE_FILES:
            return False
        added = self.added_lines.get(path, [])
        removed = self.removed_lines.get(path, [])
        if not added or len(added) != len(removed):
            return False
        return all(
            VERSION_MARKER in new
            and SEMVER_RE.search(new)
            and SEMVER_RE.sub("", new) == SEMVER_RE.sub("", old)
            for old, new in zip(removed, added, strict=True)
        )

    def code_changed(self) -> set[str]:
        """Changed paths, less release-please version bumps."""
        return {p for p in self.changed if not self.version_bump_only(p)}


@dataclass
class Result:
    name: str
    level: str  # PASS | WARN | FAIL
    detail: str


def _module_of(path: str) -> str | None:
    if not path.startswith(PKG):
        return None
    head = path[len(PKG) :].split("/", 1)
    if len(head) == 1:
        return head[0][:-3] if head[0].endswith(".py") else None
    return head[0]


def check_arch_drift(c: Change) -> Result:
    name = "arch-doc-drift"
    touched = {
        p[len(PREBUILT) : -len(".json")]
        for p in c.changed
        if p.startswith(PREBUILT) and p.endswith(".json") and "/" not in p[len(PREBUILT) :]
    }
    modules = sorted({m for p in c.code_changed() if (m := _module_of(p)) and m not in NON_DOC})
    if not modules:
        return Result(name, "PASS", "no documented module changed")
    new_modules = {
        m
        for p in c.added | c.renamed
        if p.startswith(PKG) and p.endswith("/__init__.py") and p.count("/") == 4
        for m in [_module_of(p)]
        if m and m not in NON_DOC
    }

    missing: list[str] = []
    for m in modules:
        if m in new_modules:
            needs = [f"prebuilt/{m}.json"] if m not in touched else []
            needs += [p.rsplit("/", 1)[-1] for p in (SECTIONS_PY, ARCH_PAGE) if p not in c.changed]
            if needs:
                missing.append(f"{m} (new module, also needs: {', '.join(needs)})")
            continue
        expect = SECTIONS_FOR.get(m)
        if expect is None:
            if not touched:
                missing.append(f"{m} (no section edited)")
        elif not expect & touched:
            missing.append(f"{m} (edit one of: {', '.join(sorted(expect))})")

    if not missing:
        return Result(name, "PASS", f"modules={modules} sections={sorted(touched)}")
    if waived := WAIVER_RE.search(c.waiver_text):
        waiver = waived.group(0).strip()
        return Result(name, "PASS", f"waived ({waiver}); would need: {'; '.join(missing)}")
    return Result(
        name,
        "FAIL",
        "architecture docs not updated for: "
        + "; ".join(missing)
        + f". Edit {PREBUILT}<section>.json (and architecture-facts.yaml), or add "
        "'Arch-Docs: n/a - <reason>' to a commit message or the PR description.",
    )


def check_no_stubs(c: Change) -> Result:
    hits = [
        f"{path}: {line.strip()[:80]}"
        for path, lines in sorted(c.added_lines.items())
        if path.endswith(CODE_EXT) and path not in STUB_EXEMPT
        for line in lines
        if STUB_RE.search(line)
    ]
    if hits:
        return Result("no-stubs", "FAIL", "stub markers in added lines: " + " | ".join(hits[:5]))
    return Result("no-stubs", "PASS", "no stub markers added")


def check_eval_scenarios(c: Change) -> Result:
    new_agent = any(
        p.startswith(AGENTS) and p.endswith(".py") and not p.rsplit("/", 1)[-1].startswith("_")
        for p in c.added
    )
    prompt_change = DOMAIN_PROMPTS in c.changed
    if not (new_agent or prompt_change):
        return Result("eval-scenarios", "PASS", "no new agent or domain-prompt change")
    if any(p.startswith(SCENARIOS) for p in c.changed):
        return Result("eval-scenarios", "PASS", "eval scenarios updated")
    why = "new specialist agent" if new_agent else "domain_prompts.py changed"
    return Result(
        "eval-scenarios",
        "FAIL",
        f"{why} but nothing under {SCENARIOS} changed "
        "(see CLAUDE.md -> 'Adding a New Specialist Agent').",
    )


def check_tests_present(c: Change) -> Result:
    code = [
        p
        for p in c.code_changed()
        if p.startswith(PKG) and p.endswith(".py") and _module_of(p) not in NON_DOC
    ]
    if not code or any(p.startswith(TESTS) for p in c.changed):
        return Result("tests-present", "PASS", "tests changed or no code changed")
    return Result(
        "tests-present",
        "WARN",
        f"{len(code)} openexecutive/ file(s) changed with no change under {TESTS} "
        "- fine for a pure refactor, otherwise add tests.",
    )


CHECKS = (check_arch_drift, check_no_stubs, check_eval_scenarios, check_tests_present)


def run_checks(c: Change) -> list[Result]:
    return [check(c) for check in CHECKS]


# --- git plumbing -----------------------------------------------------------


def _git(*args: str) -> str:
    # Pin the output format against user config (quotePath, noprefix, renames,
    # external diff tools), and never crash on a non-UTF-8 file.
    cmd = ["git", "-c", "core.quotePath=false", *args]
    return subprocess.run(
        cmd, check=True, capture_output=True, encoding="utf-8", errors="replace"
    ).stdout


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path with a tab, quote or backslash."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    raw = codecs.escape_decode(path[1:-1].encode("utf-8"))[0]
    return raw.decode("utf-8", errors="replace")


def _parse_added_lines(diff: str) -> dict[str, list[str]]:
    return _parse_diff_lines(diff, "+")


def _parse_removed_lines(diff: str) -> dict[str, list[str]]:
    """Removed lines, keyed by the file's new path (a deleted file has none)."""
    return _parse_diff_lines(diff, "-")


def _parse_diff_lines(diff: str, sign: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current: str | None = None
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header, current = True, None
        elif in_header and line.startswith("+++ "):
            # git ends a path containing a space with a tab
            target = _unquote(line[4:].rstrip("\t"))
            current = target[2:] if target.startswith("b/") else None
        elif line.startswith("@@"):
            in_header = False
        elif current and not in_header and line.startswith(sign):
            out.setdefault(current, []).append(line[1:])
    return out


def collect(base: str, extra_waiver: Iterable[str] = ()) -> Change:
    """Diff the working tree (committed + uncommitted + untracked) against base."""
    merge_base = _git("merge-base", base, "HEAD").strip()
    changed: set[str] = set()
    added: set[str] = set()
    renamed: set[str] = set()
    fields = _git("diff", "-z", "--name-status", "-M", merge_base).split("\0")
    i = 0
    while i < len(fields) - 1:
        status = fields[i]
        n = 2 if status[:1] in ("R", "C") else 1
        paths = fields[i + 1 : i + 1 + n]
        i += 1 + n
        changed.update(paths)
        if status.startswith("A"):
            added.add(paths[-1])
        elif status.startswith(("R", "C")):
            renamed.add(paths[-1])
    diff = _git(
        "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
        "--src-prefix=a/", "--dst-prefix=b/", merge_base,
    )
    added_lines = _parse_added_lines(diff)
    removed_lines = _parse_removed_lines(diff)

    for path in _git("ls-files", "-z", "--others", "--exclude-standard").split("\0"):
        if not path:
            continue
        changed.add(path)
        added.add(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                added_lines[path] = fh.read().splitlines()
        except OSError:
            pass

    waiver = _git("log", "--format=%B", f"{merge_base}..HEAD")
    return Change(
        changed, added, renamed, added_lines, removed_lines, "\n".join([waiver, *extra_waiver])
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="origin/main", help="ref to diff against")
    args = ap.parse_args(argv)

    try:
        change = collect(args.base, [os.environ.get("PR_BODY", "")])
    except subprocess.CalledProcessError as exc:
        print(f"pr_checks: git {' '.join(exc.cmd[1:])} failed: {exc.stderr.strip()}")
        print(f"pr_checks: is '{args.base}' fetched? try `git fetch origin main`")
        return 2

    results = run_checks(change)
    for r in results:
        print(f"{r.level:<4}  {r.name:<15} {r.detail}")
    return 1 if any(r.level == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
