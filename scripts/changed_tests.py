"""Print the test files that cover what changed on this branch.

    python3 scripts/changed_tests.py [--base origin/main]

The fast inner loop: edit, run these, repeat. `make check` still runs the whole
suite once before the first push. A test file is picked when it is itself
changed, or when its source mentions the dotted module path of a changed
package file (`openexecutive.memory.episodic`), including the package that
re-exports it. A change to shared test or build config selects everything.
Prints nothing when no source file under packages/core changed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "packages" / "core"
TEST_DIRS = ("tests/unit", "tests/integration")
# These affect every test, so a change to one runs the whole suite.
RUN_ALL = ("packages/core/tests/conftest.py", "packages/core/pyproject.toml", "packages/core/uv.lock")


def git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def changed_files(base: str) -> list[str]:
    merge_base = git("merge-base", base, "HEAD")[0]
    files = set(git("diff", "--name-only", merge_base))
    files.update(git("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def dotted(path: str) -> str:
    """packages/core/openexecutive/a/b.py -> openexecutive.a.b (a/__init__.py -> openexecutive.a)."""
    parts = Path(path).relative_to("packages/core").with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def select(files: list[str]) -> list[str]:
    if any(f in RUN_ALL for f in files):
        return [d for d in TEST_DIRS]
    picked: set[str] = set()
    modules: list[str] = []
    for f in files:
        if f.startswith("packages/core/tests/") and f.endswith(".py") and Path(ROOT / f).exists():
            if Path(f).name.startswith("test_"):
                picked.add(str(Path(f).relative_to("packages/core")))
        elif f.startswith("packages/core/openexecutive/") and f.endswith(".py"):
            modules.append(dotted(f))
    if modules:
        for d in TEST_DIRS:
            for test in sorted((CORE / d).glob("test_*.py")):
                text = test.read_text(encoding="utf-8", errors="ignore")
                if any(m in text for m in modules):
                    picked.add(str(test.relative_to(CORE)))
    return sorted(picked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    for path in select(changed_files(args.base)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
