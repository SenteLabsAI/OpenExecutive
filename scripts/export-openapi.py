#!/usr/bin/env python3
"""Write the API's OpenAPI description to a file, without running the API or
holding any of its secrets: one of the build outputs a host app checks its
pinned endpoints against (`make embed-outputs`, docs/auth.md "Host origins").

    uv run --project packages/core python scripts/export-openapi.py dist/embed/openapi.json
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages" / "core"))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    # The app's settings insist on these; the description depends on neither.
    os.environ.setdefault("EXEC_EMAIL_ADDRESS", "openapi-export@example.invalid")
    os.environ.setdefault("ANTHROPIC_API_KEY", "not-used")
    from openexecutive.api.main import create_app

    out = pathlib.Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
