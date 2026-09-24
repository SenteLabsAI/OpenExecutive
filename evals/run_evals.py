#!/usr/bin/env python3
"""Eval runner for Open Executive.

Usage:
    python run_evals.py --scenarios scenarios/ --output results/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "core"))


async def run_eval(scenario: dict, executive, session_factory) -> dict:
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.orchestrator.session import Session

    ctx = scenario.get("company_context", {})
    profile = CompanyProfile(
        name=ctx.get("name", "Eval Company"),
        industry=ctx.get("industry", ""),
        stage=ctx.get("stage", ""),
        headcount=ctx.get("headcount"),
        annual_revenue_arr=ctx.get("arr"),
    )

    if ctx.get("monthly_burn"):
        profile.financials.burn_rate_monthly = ctx["monthly_burn"]
    if ctx.get("runway_months"):
        profile.financials.runway_months = ctx["runway_months"]

    session = Session(company_profile=profile)

    response = await executive.chat(
        user_message=scenario["query"],
        session=session,
    )

    return {
        "id": scenario["id"],
        "domain": scenario["domain"],
        "query": scenario["query"],
        "response": response,
        "response_length": len(response),
    }


async def judge_response(scenario: dict, response: str, client) -> dict:
    judge_prompt = f"""You are an evaluator for an AI executive advisory system.

Evaluate this response to an executive question on a 1-5 scale for each dimension.

QUESTION: {scenario['query']}

RESPONSE: {response}

Expected topics to cover: {', '.join(scenario.get('expected_topics', []))}

Rate each dimension (1=poor, 3=acceptable, 5=excellent):
1. persona_coherence: Does it sound like a senior executive, not a generic AI?
2. domain_accuracy: Is the advice factually correct and professionally sound?
3. actionability: Does it give concrete next steps with clear recommendations?
4. topic_coverage: Does it address the expected topics?
5. specificity: Is it specific to the situation, not generic advice?
6. concision: Is the length proportional to the question? Penalize padding, restating the question, and unsolicited closing offers. A genuinely complex question earns its length.
7. overall: Your holistic assessment of the response.

Respond in JSON format:
{{"persona_coherence": N, "domain_accuracy": N, "actionability": N, "topic_coverage": N, "specificity": N, "concision": N, "overall": N, "notes": "brief explanation"}}"""

    message = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=500,
        messages=[{"role": "user", "content": judge_prompt}],
    )

    text = message.content[0].text
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"overall": 0, "notes": "Failed to parse judge response"}


async def run_workflow_eval(scenario: dict, store) -> dict:
    """Runs a workflow scenario through WORKFLOW_REGISTRY and returns the
    rendered artifact. The runner is intentionally minimal — it does not
    inject the scenario's ``company_context`` because workflows load their
    profile from ``COMPANY_PROFILE_PATH``; the scenario's ``workflow_inputs``
    already include the key metrics inline, so the artifact remains
    self-contained and judgeable.
    """
    from openexecutive.workflows import WORKFLOW_REGISTRY

    workflow_name = scenario.get("workflow")
    workflow = WORKFLOW_REGISTRY.get(workflow_name) if workflow_name else None
    if workflow is None:
        raise KeyError(f"unknown workflow: {workflow_name!r}")

    inputs = workflow.input_model()(**(scenario.get("workflow_inputs") or {}))

    artifact = ""
    async for event in workflow.run(inputs, store):
        if event.type == "artifact":
            artifact = event.content or ""
        elif event.type == "error":
            raise RuntimeError(f"workflow {workflow_name} errored: {event.message}")

    return {
        "id": scenario["id"],
        "domain": scenario["domain"],
        "workflow": workflow_name,
        "artifact": artifact,
        "artifact_length": len(artifact),
    }


async def judge_workflow(scenario: dict, artifact: str, client) -> dict:
    expected_sections = scenario.get("expected_artifact_sections", []) or []
    quality_criteria = scenario.get("quality_criteria", {}) or {}

    judge_prompt = f"""You are evaluating an artifact produced by an AI executive workflow.

WORKFLOW: {scenario.get('workflow')}
DESCRIPTION: {scenario['description']}

ARTIFACT (Markdown):
{artifact}

Expected sections (should appear as headings or clear sections):
{', '.join(expected_sections) or 'n/a'}

Quality criteria the artifact should satisfy:
{json.dumps(quality_criteria, indent=2)}

Rate each dimension 1-5 (1=poor, 3=acceptable, 5=excellent):
1. structure: All expected sections present and well-organized.
2. specificity: Uses concrete facts from the inputs; no fabricated numbers.
3. actionability: Recommendations and decisions are clear and concrete.
4. coherence: Reads as a unified document, not stitched fragments.
5. completeness: Each section is substantive, not a stub.

Respond in JSON:
{{"structure": N, "specificity": N, "actionability": N, "coherence": N, "completeness": N, "overall": N, "notes": "brief"}}"""

    message = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=500,
        messages=[{"role": "user", "content": judge_prompt}],
    )

    text = message.content[0].text
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"overall": 0, "notes": "Failed to parse judge response"}


async def run_triage_eval(scenario: dict, client) -> dict:
    """Runs a triage scenario directly against the TriageAgent (no Executive loop).

    Scenario YAML must include `event` + `context` (with `recent_alerts`,
    `muted_topics`, `active_initiatives`).
    """
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.alerts.models import AlertEvent

    # Pydantic alias `from` → AlertEvent.from_ is handled automatically.
    event = AlertEvent(**scenario["event"])

    context = scenario.get("context", {}) or {}
    recent = context.get("recent_alerts", []) or []
    mutes = context.get("muted_topics", []) or []
    initiatives = context.get("active_initiatives", []) or []

    agent = TriageAgent()
    decision = await agent.triage(
        event,
        recent_alerts=recent,
        mute_patterns=mutes,
        active_initiatives=initiatives,
    )

    return {
        "id": scenario["id"],
        "domain": scenario["domain"],
        "decision": decision.model_dump(mode="json"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenarios",
        default="../packages/core/openexecutive/evals/_scenarios/",
        help="Path to scenario YAML files (moved into the package; pass an "
        "explicit path to use a custom set)",
    )
    parser.add_argument("--output", default="results/", help="Output directory for results")
    parser.add_argument("--scenario-id", help="Run only this scenario ID")
    parser.add_argument(
        "--triage",
        action="store_true",
        help="Run triage scenarios via TriageAgent + triage_judge (skip the Executive loop)",
    )
    parser.add_argument(
        "--workflow",
        action="store_true",
        help="Run type=workflow scenarios via WORKFLOW_REGISTRY (skip the Executive loop)",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help=(
            "Run requires_mcp scenarios. CI does not configure a gateway, "
            "so they are skipped unless this flag is set."
        ),
    )
    args = parser.parse_args()
    mode_flags = [args.triage, args.workflow, args.mcp]
    if sum(mode_flags) > 1:
        parser.error("--triage, --workflow, and --mcp are mutually exclusive")

    os.environ.setdefault("COMPANY_PROFILE_PATH", "./company/profile.yaml")
    os.environ.setdefault("VECTOR_STORE_PATH", "./chroma_db")

    # The chat path's RAG layer reads ``review_items`` from the shared
    # episodic_memory.db, and the workflow path additionally touches
    # ``decisions`` / ``agent_overrides``. The API server's lifespan
    # initializes these schemas; the eval runner must do the same or every
    # specialist consult will explode with ``no such table``.
    from openexecutive.agents.overrides import initialize_overrides_db
    from openexecutive.knowledge.review_store import ReviewStore
    from openexecutive.memory.episodic import initialize_db as initialize_episodic_db

    initialize_episodic_db()
    initialize_overrides_db()
    ReviewStore.initialize_db()

    # Built through the app's factory so an organisation-scoped key gets its
    # `anthropic-workspace-id` header here too — a bare AsyncAnthropic() 400s
    # on every judge call for those keys (#128).
    from openexecutive.providers.anthropic_provider import configured_async_client

    client = configured_async_client()

    scenario_dir = Path(args.scenarios)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    def _scenario_kind(s: dict) -> str:
        if s.get("domain") == "triage":
            return "triage"
        if s.get("type") == "workflow":
            return "workflow"
        if s.get("requires_mcp"):
            return "mcp"
        return "chat"

    if args.triage:
        target_kind = "triage"
    elif args.workflow:
        target_kind = "workflow"
    elif args.mcp:
        target_kind = "mcp"
    else:
        target_kind = "chat"

    scenarios = []
    for yaml_file in sorted(scenario_dir.glob("*.yaml")):
        with open(yaml_file) as f:
            scenario = yaml.safe_load(f)
        if args.scenario_id and scenario["id"] != args.scenario_id:
            continue
        if _scenario_kind(scenario) != target_kind:
            continue
        scenarios.append(scenario)

    print(f"Running {len(scenarios)} eval scenarios ({target_kind})...")

    results = []

    if target_kind == "triage":
        from judges.triage_judge import judge_triage  # type: ignore[import-not-found]

        for scenario in scenarios:
            print(f"  [{scenario['id']}] {scenario['description']}")
            try:
                result = await run_triage_eval(scenario, client)
                scores = await judge_triage(scenario, result["decision"], client)
                result["scores"] = scores
                result["passed"] = scores.get("overall", 0) >= 3.5
                results.append(result)
                status = "PASS" if result["passed"] else "FAIL"
                print(f"    → {status} (overall: {scores.get('overall', 0):.1f}/5)")
            except Exception as e:
                print(f"    → ERROR: {e}")
                results.append({"id": scenario["id"], "error": str(e), "passed": False})
    elif target_kind == "workflow":
        from openexecutive.config import get_settings
        from openexecutive.knowledge.store import ChromaDBStore

        store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
        for scenario in scenarios:
            print(f"  [{scenario['id']}] {scenario['description']}")
            try:
                result = await run_workflow_eval(scenario, store)
                scores = await judge_workflow(scenario, result["artifact"], client)
                result["scores"] = scores
                result["passed"] = scores.get("overall", 0) >= 3.5
                results.append(result)
                status = "PASS" if result["passed"] else "FAIL"
                print(f"    → {status} (overall: {scores.get('overall', 0):.1f}/5)")
            except Exception as e:
                print(f"    → ERROR: {e}")
                results.append({"id": scenario["id"], "error": str(e), "passed": False})
    else:
        # "chat" (default) and "mcp" both go through the Executive.
        from openexecutive.orchestrator.executive import Executive

        executive = Executive()
        for scenario in scenarios:
            print(f"  [{scenario['id']}] {scenario['description']}")
            try:
                result = await run_eval(scenario, executive, None)
                scores = await judge_response(scenario, result["response"], client)
                result["scores"] = scores
                result["passed"] = scores.get("overall", 0) >= 3.5
                results.append(result)
                status = "PASS" if result["passed"] else "FAIL"
                print(f"    → {status} (overall: {scores.get('overall', 0):.1f}/5)")
            except Exception as e:
                print(f"    → ERROR: {e}")
                results.append({"id": scenario["id"], "error": str(e), "passed": False})

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"eval_results_{target_kind}_{timestamp}.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

    passed = sum(1 for r in results if r.get("passed", False))
    print(f"\nResults: {passed}/{len(results)} passed")
    print(f"Output: {output_file}")

    if passed < len(results) * 0.8:
        print("WARNING: Less than 80% of evals passed")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
