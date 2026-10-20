"""record_decision_outcome (orchestrator/decision_tools.py): records how a
past decision turned out — the principal on a verified surface only, and
never in an unattended run."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openexecutive.memory import episodic
from openexecutive.orchestrator import decision_tools
from openexecutive.orchestrator.decision_tools import (
    RECORD_DECISION_OUTCOME_TOOL,
    handle_record_decision_outcome,
)

_REAL_PRINCIPAL_ASKED = decision_tools._principal_asked


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from openexecutive.audit import AuditLogger, set_audit_logger

    db = tmp_path / "decisions.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    set_audit_logger(AuditLogger(db_path=db))
    # Most tests speak as the verified principal; the gate has its own test.
    monkeypatch.setattr(decision_tools, "_principal_asked", lambda: True)
    yield db
    set_audit_logger(None)


def _decision(db: Path, summary: str, *, days_ago: float = 40, outcome: str = "") -> int:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        cur = conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary, outcome) VALUES (?, ?, ?, ?)",
            (ts, "strategy", summary, outcome),
        )
        return int(cur.lastrowid or 0)


def _call(**payload: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(handle_record_decision_outcome(payload)))


def test_records_the_outcome_and_audits_it(_isolated: Path) -> None:
    did = _decision(_isolated, "Replace hourly billing with sprint packages")
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        result = _call(decision_id=did, outcome="Worked — 3 of 3 new clients took sprints.",
                       rationale="The principal said all three new clients chose sprints.")
    assert result["status"] == "ok" and result["decision_id"] == did
    assert result["replaced_previous"] is False
    stored = episodic.get_decision(did, db_path=_isolated)
    assert stored is not None and stored.outcome == "Worked — 3 of 3 new clients took sprints."
    ok = [c.kwargs["details"] for c in fake_log.call_args_list
          if c.kwargs.get("details", {}).get("ok") is True]
    assert len(ok) == 1
    assert ok[0]["tool"] == "record_decision_outcome" and ok[0]["decision_id"] == did
    assert ok[0]["rationale"].startswith("The principal said")
    # The decision no longer waits on an outcome.
    assert episodic.decisions_awaiting_outcome(datetime.now(UTC)) == []


def test_replacing_an_outcome_keeps_the_old_one_in_the_audit(_isolated: Path) -> None:
    did = _decision(_isolated, "Hire a contractor", outcome="Too early to tell")
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        result = _call(decision_id=did, outcome="Paid off", rationale="Said it paid off.")
    assert result["replaced_previous"] is True
    details = next(c.kwargs["details"] for c in fake_log.call_args_list
                   if c.kwargs.get("details", {}).get("ok") is True)
    assert details["previous_outcome"] == "Too early to tell"


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"outcome": " ", "rationale": "r"}, "outcome is required"),
        ({"outcome": "x" * 1001, "rationale": "r"}, "at most 1000"),
        ({"outcome": "worked", "rationale": ""}, "rationale is required"),
    ],
)
def test_validates_input(_isolated: Path, payload: dict[str, Any], fragment: str) -> None:
    did = _decision(_isolated, "A decision")
    result = _call(decision_id=did, **payload)
    assert fragment in result["error"]
    stored = episodic.get_decision(did, db_path=_isolated)
    assert stored is not None and stored.outcome == ""


@pytest.mark.parametrize("bad_id", [None, "abc", 0, -3, True, 999])
def test_a_missing_or_unknown_id_lists_the_candidates(_isolated: Path, bad_id: Any) -> None:
    older = _decision(_isolated, "Older call", days_ago=60)
    newer = _decision(_isolated, "Newer call", days_ago=2)
    _decision(_isolated, "Answered call", days_ago=50, outcome="fine")
    payload: dict[str, Any] = {"outcome": "worked", "rationale": "r"}
    if bad_id is not None:
        payload["decision_id"] = bad_id
    result = _call(**payload)
    assert "error" in result
    assert [c["decision_id"] for c in result["awaiting_outcome"]] == [newer, older]


def test_store_failures_reach_the_model_as_the_type_only(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    did = _decision(_isolated, "A decision")
    secret = "/var/data/tenant-7/episodic.db is locked"

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(episodic, "update_decision", boom)
    result = _call(decision_id=did, outcome="worked", rationale="r")
    assert "RuntimeError" in result["error"] and "tenant-7" not in result["error"]


# --------------------------------------------------------------------------- #
# Who may record one
# --------------------------------------------------------------------------- #


@pytest.fixture
def roster(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    monkeypatch.setattr(decision_tools, "_principal_asked", _REAL_PRINCIPAL_ASKED)
    monkeypatch.setattr(people_store, "DB_PATH", _isolated)
    people_store.initialize_db(_isolated)
    people_registry.invalidate()
    yield SimpleNamespace(
        principal=people_store.upsert_person(full_name="Pat Lee", is_principal=True),
        teammate=people_store.upsert_person(full_name="Sam Ortiz"),
    )
    people_registry.invalidate()


def test_only_the_principal_on_a_verified_surface_may_record(
    _isolated: Path, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.orchestrator.session import Session

    monkeypatch.setattr(type(get_settings()), "telegram_webhook_secret_valid", False,
                        raising=False)
    did = _decision(_isolated, "A decision")
    refused_sessions = {
        "no session (CLI, MCP server)": None,
        "email from the principal's address": Session(caller_person_id=roster.principal),
        "Google Chat": Session(origin_channel="google_chat", origin_channel_ref="spaces/x",
                               caller_person_id=roster.principal),
        "a teammate in the web chat": Session(from_web_chat=True, caller_person_id=roster.teammate),
        "a teammate on Slack": Session(origin_channel="slack", origin_channel_ref="U2",
                                       caller_person_id=roster.teammate),
        "the scheduler's proactive trigger": Session(unattended=True),
    }
    fake_log = MagicMock()
    with patch("openexecutive.audit.log_event", fake_log):
        for label, session in refused_sessions.items():
            with set_session(session):
                result = _call(decision_id=did, outcome="worked", rationale="r")
            assert "refused" in result.get("error", ""), (label, result)
    stored = episodic.get_decision(did, db_path=_isolated)
    assert stored is not None and stored.outcome == ""
    refusals = [c.kwargs["details"] for c in fake_log.call_args_list
                if c.kwargs.get("details", {}).get("refused") is True]
    assert len(refusals) == len(refused_sessions)
    assert {"caller_person_id", "origin_channel", "from_web_chat", "unattended"} <= set(refusals[0])

    with set_session(Session(from_web_chat=True, caller_person_id=roster.principal)):
        assert _call(decision_id=did, outcome="worked", rationale="r")["status"] == "ok"


# --------------------------------------------------------------------------- #
# Registration: static schema, sorted, withheld from unattended runs
# --------------------------------------------------------------------------- #


def test_registered_with_a_static_schema_in_a_sortable_list() -> None:
    from openexecutive.orchestrator.activity_labels import _LABELS
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS

    names = [t["name"] for t in _ALL_SKILL_TOOLS]
    assert "record_decision_outcome" in names and len(names) == len(set(names))
    assert _ALL_SKILL_HANDLERS["record_decision_outcome"] is handle_record_decision_outcome
    schema = RECORD_DECISION_OUTCOME_TOOL["input_schema"]
    assert schema["required"] == ["decision_id", "outcome", "rationale"]
    assert all("enum" not in p for p in schema["properties"].values())
    assert "record_decision_outcome" in _LABELS


@pytest.mark.parametrize("mode", ["solo", "team"])
def test_not_offered_to_the_unattended_passes(mode: str) -> None:
    from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS, _ALL_SKILL_TOOLS
    from openexecutive.orchestrator.schedule_tools import (
        UNATTENDED_WITHHELD_TOOLS,
        unattended_toolkit,
    )

    assert "record_decision_outcome" in UNATTENDED_WITHHELD_TOOLS
    tools, handlers = unattended_toolkit(list(_ALL_SKILL_TOOLS), dict(_ALL_SKILL_HANDLERS), mode)
    assert "record_decision_outcome" not in {t["name"] for t in tools}
    assert "record_decision_outcome" not in handlers


def test_chip_names_the_decision_and_links_memories() -> None:
    from openexecutive.orchestrator.action_chips import summarize_action

    chip = summarize_action(
        tool_name="record_decision_outcome",
        tool_input={"decision_id": 4, "outcome": "worked"},
        tool_result=json.dumps({"status": "ok", "decision_id": 4,
                                "decision": "Drop hourly billing", "outcome": "worked"}),
    )
    assert chip is not None
    assert chip["summary"] == "Recorded how it turned out: Drop hourly billing"
    assert chip["link"] == "/memories" and chip["target"] == "decision 4"
    refused = summarize_action(
        tool_name="record_decision_outcome", tool_input={"decision_id": 4},
        tool_result=json.dumps({"error": "refused: only the principal"}),
    )
    assert refused is None


def test_solo_persona_teaches_it_and_the_team_persona_does_not() -> None:
    from openexecutive.prompts.executive_persona import (
        EXECUTIVE_PERSONA_PROMPT,
        EXECUTIVE_PERSONA_SOLO_PROMPT,
    )

    assert "`record_decision_outcome`" in EXECUTIVE_PERSONA_SOLO_PROMPT
    assert "record_decision_outcome" not in EXECUTIVE_PERSONA_PROMPT


def test_decision_outcome_eval_scenario_is_shipped_and_valid() -> None:
    from openexecutive.evals.scenarios import validate_scenario_yaml

    path = (Path(decision_tools.__file__).parents[1] / "evals" / "_scenarios"
            / "decision_outcome_001.yaml")
    scenario = validate_scenario_yaml(path.read_text(encoding="utf-8"))
    assert scenario["quality_criteria"]["does_not_claim_the_outcome_is_recorded"] is True
