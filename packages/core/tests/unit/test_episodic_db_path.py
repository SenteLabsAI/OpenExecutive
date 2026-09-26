"""`memory/episodic.py` looks `DB_PATH` up when a function runs, not at import.

A `db_path: Path = DB_PATH` default is evaluated once, at import, so a test
that patches `episodic.DB_PATH` never reached a function declared that way: it
kept reading `./episodic_memory.db` in the working directory, and failed with
"no such table" whenever a partial copy of that file was lying around.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from openexecutive.memory import episodic


def test_format_for_prompt_reads_the_patched_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "episodic.db"
    episodic.initialize_db(db_path)
    episodic.store_decision("strategy", "Open a second warehouse in Leeds", db_path=db_path)
    monkeypatch.setattr(episodic, "DB_PATH", db_path)

    assert "Open a second warehouse in Leeds" in episodic.format_for_prompt()


def test_no_db_path_default_is_bound_at_import() -> None:
    """Guards the pattern rather than one function, so a new
    `db_path: Path = DB_PATH` cannot bring the trap back."""
    defaults = {
        name: inspect.signature(fn).parameters["db_path"].default
        for name, fn in inspect.getmembers(episodic, inspect.isfunction)
        if fn.__module__ == episodic.__name__
        and "db_path" in inspect.signature(fn).parameters
    }
    # Not vacuous: `_get_conn` is wrapped by @contextmanager, and signature()
    # follows its __wrapped__ to the real parameters.
    assert {"_get_conn", "initialize_db", "format_for_prompt"} <= defaults.keys()
    bound = sorted(
        name
        for name, default in defaults.items()
        if default is not None and default is not inspect.Parameter.empty
    )
    assert bound == []
