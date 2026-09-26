#!/bin/bash
# PostToolUse: lint a Python file under packages/core right after Claude edits
# it, so errors surface at the edit instead of in CI. Exit 2 feeds ruff's
# output back to Claude. Unused-name rules are skipped: mid-edit, an import
# often lands one edit before its use.
set -uo pipefail

file=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)
core="$CLAUDE_PROJECT_DIR/packages/core"
ruff="$core/.venv/bin/ruff"

case "$file" in
  "$core"/*.py) ;;
  *) exit 0 ;;
esac
[ -x "$ruff" ] && [ -f "$file" ] || exit 0

if ! out=$(cd "$core" && "$ruff" check --quiet --extend-ignore F401,F841 "$file" 2>&1); then
  echo "$out" >&2
  exit 2
fi
