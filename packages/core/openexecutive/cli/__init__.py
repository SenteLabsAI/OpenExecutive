"""Click CLI package.

The Click app lives in the sibling module ``openexecutive/cli.py``. This
directory otherwise shadows that file for ``import openexecutive.cli``, which
is the console-script entry point (``openexecutive.cli:cli``). Load the
sibling file under a unique module name and re-export ``cli``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_CLICK_APP_PATH = Path(__file__).resolve().parent.parent / "cli.py"
_spec = importlib.util.spec_from_file_location(
    "openexecutive._click_cli",
    _CLICK_APP_PATH,
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load Click CLI from {_CLICK_APP_PATH}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
cli = _mod.cli

__all__ = ["cli"]
