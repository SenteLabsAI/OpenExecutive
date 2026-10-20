"""Functions for test_isolated.py to run in the child process.

Standard library only: the child imports this module by name, and tests run
from ``packages/core``, so ``tests.unit._isolated_helpers`` resolves there.
"""
from __future__ import annotations

import os
import sys


def flood_stderr_and_die(megabytes: int) -> None:
    """Write ``megabytes`` MB to stderr, then exit without answering."""
    block = "x" * (1024 * 1024)
    for _ in range(megabytes):
        sys.stderr.write(block)
    sys.stderr.flush()
    os._exit(5)


def echo(value: str) -> str:
    return value
