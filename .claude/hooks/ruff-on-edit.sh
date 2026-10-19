#!/bin/bash
# PostToolUse: lint a Python file under packages/core right after Claude edits
# it, so errors surface at the edit instead of in CI. Exit 2 feeds ruff's
# output back to Claude. Unused-name rules are skipped: mid-edit, an import
# often lands one edit before its use.
#
# Works on Windows too (Git Bash): Python decides whether the file is under
# packages/core, since the paths there are Windows paths (C:\...) that a shell
# glob can't compare, and the venv keeps ruff in Scripts/ruff.exe, not bin/.
set -uo pipefail

py=$(command -v python3 || command -v python) || exit 0
# Prints packages/core and the edited file, one per line, only for a .py file
# inside packages/core; nothing otherwise.
paths=$("$py" -c '
import json, os, sys
core = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), "packages", "core")
path = json.load(sys.stdin).get("tool_input", {}).get("file_path", "")
if not path:
    sys.exit()
core, path = os.path.realpath(core), os.path.realpath(path)
try:
    inside = os.path.commonpath([os.path.normcase(core), os.path.normcase(path)]) == os.path.normcase(core)
except ValueError:  # different drives
    inside = False
if inside and path.endswith(".py") and os.path.isfile(path):
    print(core)
    print(path)
' 2>/dev/null) || exit 0
[ -n "$paths" ] || exit 0
core=$(printf '%s\n' "$paths" | sed -n 1p)
file=$(printf '%s\n' "$paths" | sed -n 2p)

ruff=""
for candidate in "$core/.venv/bin/ruff" "$core/.venv/Scripts/ruff.exe"; do
  if [ -x "$candidate" ]; then
    ruff=$candidate
    break
  fi
done
[ -n "$ruff" ] || exit 0

if ! out=$(cd "$core" && "$ruff" check --quiet --extend-ignore F401,F841 "$file" 2>&1); then
  echo "$out" >&2
  exit 2
fi
