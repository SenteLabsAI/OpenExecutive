"""Plain names and outcome lines for finished runs (workflows/run_outcome.py)."""
from __future__ import annotations

from typing import Any

import pytest

from openexecutive.workflows import run_outcome
from openexecutive.workflows.executive_research import _render_artifact
from openexecutive.workflows.persistence import PRIVATE_RUN_ARTIFACT


def _run(name: str, title: str = "", artifact: str = "", **inputs: Any) -> dict[str, Any]:
    return {"run_id": "r1", "workflow_name": name, "title": title,
            "artifact": artifact, "inputs": inputs}


def _research(findings: int, calls: list[dict[str, Any]]) -> str:
    from types import SimpleNamespace

    finding = SimpleNamespace(
        severity_hint=SimpleNamespace(value="medium"), confidence="high",
        source_specialist="market", title="Price rise", summary="Supplier raising prices",
    )
    return _render_artifact(
        deduped=[finding] * findings, per_specialist=[], tool_calls=calls,
        narrative="**Acted on:** stuff", note="initial post-onboarding research run",
    )


@pytest.mark.parametrize(
    ("name", "title", "expected"),
    [
        ("executive_research", "Executive Research (post-onboarding auto-fire)",
         "Researched your market"),
        ("executive_research", "Executive Research (periodic)", "Researched your market"),
        ("morning_brief", "Morning Brief 2026-10-17", "Sent the morning brief"),
        ("morning_brief", "Overnight brief — acme", "Sent the morning brief"),
        ("weekly_review", "Weekly Review 2026-10-17", "Wrote the weekly review"),
        ("competitive_teardown", "Teardown of Acme", "Teardown of Acme"),
        ("board_prep", "Board Prep (chat-tool fire)", "Board Prep"),
        ("board_prep", "", "Board prep deck"),
    ],
)
def test_plain_name_never_shows_the_log_title(name: str, title: str, expected: str) -> None:
    assert run_outcome.plain_name(_run(name, title)) == expected


def test_a_check_in_names_its_department(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from openexecutive.departments import store

    monkeypatch.setattr(
        store, "get_department",
        lambda slug: SimpleNamespace(config=SimpleNamespace(title="Sales")) if slug == "sales" else None,
    )
    assert run_outcome.plain_name(
        _run("department_check_in", "Check-in: sales 2026-W42", department_slug="sales")
    ) == "Checked in with Sales"
    # An older row without inputs still reads its slug from the title.
    assert run_outcome.plain_name(
        {"workflow_name": "department_check_in", "title": "Check-in: field_ops 2026-W42"}
    ) == "Checked in with Field Ops"


def test_research_says_what_it_did() -> None:
    artifact = _research(9, [
        {"tool": "send_slack_dm", "ok": True, "result_preview": "sent"},
        {"tool": "create_alert", "ok": True, "result_preview": "raised"},
        {"tool": "send_slack_dm", "ok": False, "result_preview": "failed"},
        {"tool": "add_watchlist_entry", "ok": True, "outcome": "added", "result_preview": "acme.com"},
        {"tool": "add_watchlist_entry", "ok": True, "outcome": "added", "result_preview": "beta.com"},
    ])
    assert run_outcome.outcome_line(_run("executive_research", artifact=artifact)) == (
        "Looked at 9 updates. Sent 1 message, flagged 1 thing for you and started watching 2 things."
    )


def test_quiet_research_says_nothing_needed_you() -> None:
    line = run_outcome.outcome_line(_run("executive_research", artifact=_research(1, [])))
    assert line == "Looked at 1 update. Nothing needed you."
    line = run_outcome.outcome_line(_run("executive_research", artifact=_research(0, [])))
    assert line == "Found nothing new."


def test_check_in_counts_its_proposals() -> None:
    artifact = (
        "# Sales — Check-In Report\n\n## Goal Status\n\nfine\n\n## Proposed Actions\n\n"
        "- **dm**: ping Dana → gate: *auto*\n- **dm**: ping Lee → gate: *auto*\n\n---\n_Generated…_\n"
    )
    assert run_outcome.outcome_line(_run("department_check_in", artifact=artifact)) == (
        "Reviewed the goals and proposed 2 actions."
    )
    quiet = artifact.replace(
        "- **dm**: ping Dana → gate: *auto*\n- **dm**: ping Lee → gate: *auto*",
        "_No actions proposed._",
    )
    assert run_outcome.outcome_line(_run("department_check_in", artifact=quiet)) == (
        "Reviewed the goals. Nothing needed action."
    )


def test_weekly_review_leads_with_next_weeks_top_item() -> None:
    artifact = (
        "# Weekly review — 2026-W42\n\n## Goals by area\n\n_x_\n\n## Next week's top 3\n\n"
        "1. Close the **Henderson** renewal — it lapses Friday\n2. Hire a rep — why\n"
    )
    assert run_outcome.outcome_line(_run("weekly_review", artifact=artifact)) == (
        "Top for next week: Close the Henderson renewal"
    )


@pytest.mark.parametrize("name", ["morning_brief", "end_of_day_digest", "executive_reflection"])
def test_briefs_and_reflections_get_no_line(name: str) -> None:
    assert run_outcome.outcome_line(_run(name, artifact="Good morning. Henderson is late.")) is None


def test_other_runs_lead_with_their_first_sentence() -> None:
    artifact = "# Board prep\n\n_Note: draft_\n\n**Revenue** grew 12% this quarter. More below.\n"
    assert run_outcome.outcome_line(_run("board_prep", artifact=artifact)) == (
        "Revenue grew 12% this quarter."
    )


@pytest.mark.parametrize("artifact", ["", "(no artifact)", PRIVATE_RUN_ARTIFACT])
def test_no_write_up_means_no_line_and_no_link(artifact: str) -> None:
    assert run_outcome.outcome_line(_run("board_prep", artifact=artifact)) is None
    assert not run_outcome.has_write_up(artifact)


def test_the_reflection_is_never_linked() -> None:
    run = _run("executive_reflection", artifact="Mail from a contact is unanswered.")
    assert run_outcome.write_up_id(run) is None
    assert run_outcome.write_up_id(_run("weekly_review", artifact="# Weekly review")) == "run:r1"


def test_a_long_line_is_clipped_at_a_word() -> None:
    line = run_outcome.outcome_line(_run("board_prep", artifact="word " * 80))
    assert line is not None and len(line) <= 160 and line.endswith("…")
