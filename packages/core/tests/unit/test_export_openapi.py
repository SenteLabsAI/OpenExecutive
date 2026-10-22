"""scripts/export-openapi.py writes the API description a host app checks
its pinned endpoints against (`make embed-outputs`), with no API running."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "export-openapi.py"


def test_it_writes_the_routes_a_host_may_call(tmp_path: Path) -> None:
    out = tmp_path / "embed" / "openapi.json"
    subprocess.run([sys.executable, str(SCRIPT), str(out)], check=True, timeout=120)
    spec = json.loads(out.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.")
    for path in ("/today", "/chat", "/watchlist/{slug}"):
        assert path in spec["paths"], path
