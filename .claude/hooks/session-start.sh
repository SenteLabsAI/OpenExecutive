#!/bin/bash
# Installs deps so lint, tests and the UI build work in Claude Code on the web.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
(cd packages/core && uv sync --extra dev --quiet)
(cd packages/ui && npm ci --no-audit --no-fund --no-update-notifier --loglevel=error)

# get_settings() has no default for this, so ad-hoc `uv run python` snippets
# fail without it (tests set their own in conftest.py).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export EXEC_EMAIL_ADDRESS="${EXEC_EMAIL_ADDRESS:-dev@example.com}"' >> "$CLAUDE_ENV_FILE"
fi
