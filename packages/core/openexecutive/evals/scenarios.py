"""Load and validate eval scenarios.

Scenarios come from two sources:
1. Built-in YAML files shipped inside the package (`_scenarios/*.yaml`).
2. User-added scenarios stored in the `eval_scenarios` SQLite table (created
   via the `/evals/scenarios` POST endpoint). These persist on the `/data`
   volume across deploys.

Built-in scenarios are read-only. User scenarios are editable/deletable via
the API. ID collisions between the two are blocked at insert time.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _scenarios_dir() -> Path:
    env_path = os.environ.get("EVAL_SCENARIOS_PATH")
    if env_path:
        return Path(env_path)
    # YAMLs ship inside the package — works for any install layout.
    return Path(__file__).parent / "_scenarios"


def scenario_kind(s: dict[str, Any]) -> str:
    if s.get("domain") == "triage":
        return "triage"
    if s.get("type") == "workflow":
        return "workflow"
    if s.get("requires_mcp"):
        return "mcp"
    return "chat"


def _load_builtin_scenarios() -> list[dict[str, Any]]:
    scenarios_dir = _scenarios_dir()
    if not scenarios_dir.exists():
        return []
    out = []
    for yaml_file in sorted(scenarios_dir.glob("*.yaml")):
        with open(yaml_file, encoding="utf-8") as f:
            s = yaml.safe_load(f)
        s["_kind"] = scenario_kind(s)
        s["_is_builtin"] = True
        out.append(s)
    return out


def _load_user_scenarios() -> list[dict[str, Any]]:
    # Imported lazily so this module stays usable without the DB layer
    # (e.g. the standalone `evals/run_evals.py` CLI).
    from openexecutive.evals.persistence import list_user_scenarios

    out = []
    for row in list_user_scenarios():
        try:
            s = yaml.safe_load(row["yaml"])
        except yaml.YAMLError:
            continue
        if not isinstance(s, dict):
            continue
        s["_kind"] = scenario_kind(s)
        s["_is_builtin"] = False
        out.append(s)
    return out


def load_scenarios(
    kind: str | None = None,
    scenario_id: str | None = None,
) -> list[dict[str, Any]]:
    all_scenarios = _load_builtin_scenarios() + _load_user_scenarios()

    results = []
    for s in all_scenarios:
        if scenario_id and s.get("id") != scenario_id:
            continue
        if kind and s["_kind"] != kind:
            continue
        results.append(s)
    return results


def list_scenario_meta() -> list[dict[str, Any]]:
    return [
        {
            "id": s["id"],
            "kind": s["_kind"],
            "domain": s.get("domain", ""),
            "description": s.get("description", ""),
            "workflow": s.get("workflow"),
            "is_builtin": s.get("_is_builtin", True),
        }
        for s in load_scenarios()
    ]


def builtin_scenario_ids() -> set[str]:
    return {s["id"] for s in _load_builtin_scenarios()}


def validate_scenario_yaml(raw: str) -> dict[str, Any]:
    """Parse and lightly validate scenario YAML. Raises ValueError on problems.

    Heavy validation (workflow inputs match the workflow's input_model, etc.)
    happens at run time and surfaces as a normal `scenario_error` event.
    """
    try:
        s = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}") from e

    if not isinstance(s, dict):
        raise ValueError("Top-level YAML must be a mapping")
    if not s.get("id") or not isinstance(s["id"], str):
        raise ValueError("Missing or non-string `id` field")
    if "/" in s["id"] or " " in s["id"]:
        raise ValueError("`id` must not contain '/' or whitespace")

    k = scenario_kind(s)
    if k == "chat" and not s.get("query"):
        raise ValueError("chat scenarios require a `query` field")
    if k == "workflow" and not s.get("workflow"):
        raise ValueError("workflow scenarios require a `workflow` field")
    if k == "triage" and not s.get("event"):
        raise ValueError("triage scenarios require an `event` field")

    return s
