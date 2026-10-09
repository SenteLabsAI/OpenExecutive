"""What a chat turn offers the model, deferred tools included.

The loop sends the less common tools through open_tools/use_tool
(orchestrator.tool_groups), so the tools in a request are not the whole
offer. ``capture_offered`` records, per loop iteration, the sorted names of
every tool the turn offered before they were split.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from openexecutive.orchestrator import tool_groups


@contextmanager
def capture_offered() -> Iterator[list[list[str]]]:
    seen: list[list[str]] = []
    real = tool_groups.split

    def spy(tools: Iterable[dict[str, Any]]) -> Any:
        listed = list(tools)
        seen.append(sorted(t["name"] for t in listed))
        return real(listed)

    with patch.object(tool_groups, "split", spy):
        yield seen
