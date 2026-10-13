"""The ready-made picks /jobs leads with name real, startable workflows.

`packages/ui/src/lib/workflowStarters.ts` lists them by WORKFLOW_REGISTRY key
(team and solo sets). The page silently drops a name the catalog lacks, so a
rename here would quietly shrink "Good places to start"; this pins them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from openexecutive.workflows import WORKFLOW_REGISTRY
from openexecutive.workflows.base import principal_only_in

_STARTERS_TS = (
    Path(__file__).resolve().parents[3] / "ui" / "src" / "lib" / "workflowStarters.ts"
)


def _pick_names(const: str) -> list[str]:
    source = _STARTERS_TS.read_text(encoding="utf-8")
    m = re.search(rf"export const {const}: StarterPick\[\] = \[(.*?)\n\];", source, re.S)
    assert m, f"{const} not found in {_STARTERS_TS}"
    return re.findall(r'name: "([a-z0-9_]+)"', m.group(1))


def test_solo_picks_are_the_owner_operator_set() -> None:
    assert _pick_names("SOLO_STARTER_PICKS") == [
        "mbr", "pricing_review", "churn_deep_dive", "quarterly_plan",
    ]


@pytest.mark.parametrize(
    ("const", "mode"),
    [("TEAM_STARTER_PICKS", "team"), ("SOLO_STARTER_PICKS", "solo")],
)
def test_picks_exist_and_anyone_can_start_them(const: str, mode: str) -> None:
    names = _pick_names(const)
    assert len(names) == 4
    for name in names:
        workflow = WORKFLOW_REGISTRY.get(name)
        assert workflow is not None, f"{name} is not in WORKFLOW_REGISTRY"
        # Listed under "All", not only under "System".
        assert not workflow.meta().background, name
        assert not principal_only_in(workflow, mode), name
