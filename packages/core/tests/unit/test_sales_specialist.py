"""The sales specialist is wired everywhere the roster is enumerated.

A specialist appears in many places that do not import the registry — the
MCP enum, the dynamic-workflow fallback, the fixture generator's allowlist,
retrieval aliases, knowledge domains, skill categories. Each drift is silent
(a missing alias retrieves EVERY domain; a missing DOMAIN_MAP entry tags the
docs `general`), so these tests pin them to one another, and check that the
sales knowledge docs actually index under `sales` and reach the specialist.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from openexecutive.agents.sales import SalesAgent
from openexecutive.config import get_settings
from openexecutive.knowledge import retriever
from openexecutive.knowledge.loader import (
    BUILTIN_KNOWLEDGE_PATH,
    DOMAIN_MAP,
    GENERAL_DOMAIN,
    UPLOAD_DOMAINS,
    infer_domain_from_path,
    ingest_builtin_file,
)
from openexecutive.knowledge.review_store import (
    ContentType,
    ReviewStatus,
    ReviewStore,
    build_item_id,
)
from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES
from openexecutive.knowledge.skills import SKILL_CATEGORIES
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.orchestrator.router import (
    SPECIALIST_DESCRIPTIONS,
    SPECIALIST_REGISTRY,
    SPECIALIST_TOOLS,
)
from openexecutive.prompts.domain_prompts import SALES_PROMPT

SALES_DOCS = {
    "sales/follow_up_and_forecasting.md",
    "sales/founder_led_sales.md",
    "sales/proposals_and_pricing_conversations.md",
    "sales/qualification_and_pipeline.md",
}
SOLO_FOUNDER_DOCS = {
    "finance/bootstrapped_cash_management.md",
    "hr/first_contractor_vs_first_employee.md",
    "legal/contractor_agreements_and_ip.md",
    "marketing/pricing_a_solo_offer.md",
}


# ── the agent and its registration ───────────────────────────────────────────


def test_sales_agent_is_registered() -> None:
    agent = SPECIALIST_REGISTRY["sales"]
    assert isinstance(agent, SalesAgent)
    assert (agent.name, agent.domain) == ("sales", "sales")
    assert agent.model == get_settings().default_model
    assert agent.use_deep_reasoning is False
    assert agent.get_system_prompt() is SALES_PROMPT


def test_consult_specialist_enum_is_the_sorted_registry() -> None:
    spec = SPECIALIST_TOOLS[0]["input_schema"]["properties"]["specialist"]
    assert spec["enum"] == sorted(SPECIALIST_REGISTRY)
    assert "sales" in spec["enum"]
    assert "sales (Head of Sales" in spec["description"]


def test_every_specialist_is_described() -> None:
    assert set(SPECIALIST_DESCRIPTIONS) == set(SPECIALIST_REGISTRY)


def test_hard_coded_rosters_track_the_registry() -> None:
    from openexecutive.fixtures.generator import ALLOWED_SPECIALIST_KEYS
    from openexecutive.mcp_server.server import specialist_keys
    from openexecutive.workflows.dynamic_models import _FALLBACK_SPECIALISTS

    assert set(specialist_keys()) == set(SPECIALIST_REGISTRY)
    assert set(_FALLBACK_SPECIALISTS) == set(SPECIALIST_REGISTRY)
    # Org-facing only: board_comms and triage are internal.
    org_facing = set(SPECIALIST_REGISTRY) - {"board_comms", "triage"}
    assert org_facing == ALLOWED_SPECIALIST_KEYS


def test_exec_search_for_a_sales_leader_consults_sales() -> None:
    from openexecutive.workflows.exec_search_brief import _resolve_function_specialist

    assert _resolve_function_specialist(" Sales ") == "sales"


def test_no_sales_department_is_seeded() -> None:
    """Existing installs keep their departments; `sales` simply has none, so
    its department-memory prefetch and Honcho sync are skipped like triage's."""
    from openexecutive.departments.charters import DEFAULT_DEPARTMENTS

    assert "sales" not in {slug for slug, *_ in DEFAULT_DEPARTMENTS}
    assert "sales" not in {key for _slug, _title, key, _charter in DEFAULT_DEPARTMENTS}


# ── knowledge domain wiring ──────────────────────────────────────────────────


def test_sales_domain_is_known_everywhere() -> None:
    assert DOMAIN_MAP["sales"] == "sales"
    assert "sales" in UPLOAD_DOMAINS
    assert retriever.DOMAIN_ALIASES["sales"] == ["sales", "marketing"]
    # skills.py: "Mirrors knowledge/loader.DOMAIN_MAP plus general".
    assert set(SKILL_CATEGORIES) == set(DOMAIN_MAP) | {GENERAL_DOMAIN}


def test_new_docs_ship_and_infer_their_domain() -> None:
    assert SALES_DOCS | SOLO_FOUNDER_DOCS <= SHIPPED_BUILTIN_FILES
    for rel in SALES_DOCS | SOLO_FOUNDER_DOCS:
        path = BUILTIN_KNOWLEDGE_PATH / rel
        assert path.is_file(), rel
        assert infer_domain_from_path(path, root=BUILTIN_KNOWLEDGE_PATH) == rel.split("/")[0]


def test_new_docs_register_as_trusted_defaults(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)

    rs = ReviewStore(db_path=db)
    for rel in SALES_DOCS | SOLO_FOUNDER_DOCS:
        domain, filename = rel.split("/")
        item = rs.get_item(build_item_id(ContentType.BUILTIN, domain, filename))
        assert item is not None, rel
        assert item.status is ReviewStatus.APPROVED, rel
        assert item.trusted_default, rel
    assert not rs.get_withheld_keys(ContentType.BUILTIN)


# ── retrieval, against a real ChromaDB store ─────────────────────────────────


@pytest.fixture
def indexed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # retrieve() audits every pass; keep those rows out of ./episodic_memory.db.
    monkeypatch.setattr(retriever, "_audit_log", lambda *a, **k: None)
    store = ChromaDBStore(persist_directory=str(tmp_path / "chroma"))
    db = tmp_path / "review.db"
    ReviewStore.initialize_db(db)
    ReviewStore.sync_builtin_registrations(db)

    async def _index() -> None:
        for rel in sorted(SALES_DOCS | SOLO_FOUNDER_DOCS):
            await ingest_builtin_file(BUILTIN_KNOWLEDGE_PATH / rel, store)

    asyncio.run(_index())
    return store, ReviewStore(db_path=db)


def _cited(text: str) -> set[str]:
    return set(re.findall(r"\[([a-z_]+\.md)\]", text))


def test_sales_specialist_retrieves_the_sales_docs(indexed) -> None:
    store, rs = indexed
    text = retriever.retrieve(
        "How should I define pipeline stages with exit criteria and qualify deals "
        "so the pipeline stops being inflated?",
        specialist_name="sales",
        store=store,
        review_store=rs,
    )

    assert "qualification_and_pipeline.md" in _cited(text)
    assert _cited(text) <= {
        "follow_up_and_forecasting.md",
        "founder_led_sales.md",
        "proposals_and_pricing_conversations.md",
        "qualification_and_pipeline.md",
        "pricing_a_solo_offer.md",  # marketing is the sales alias's second domain
    }


def test_cfo_gets_the_bootstrapped_cash_doc_and_no_sales_docs(indexed) -> None:
    store, rs = indexed
    text = retriever.retrieve(
        "I'm a bootstrapped solo founder. How do I work out runway at my current burn, "
        "set aside tax and decide how much to pay myself?",
        specialist_name="cfo",
        store=store,
        review_store=rs,
    )

    assert _cited(text) == {"bootstrapped_cash_management.md"}


# ── eval scenarios ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scenario_id", "specialist"),
    [("sales_001", "sales"), ("sales_002", "sales"), ("finance_bootstrapped_001", "cfo")],
)
def test_eval_scenarios_ship_as_chat_scenarios(
    monkeypatch: pytest.MonkeyPatch, scenario_id: str, specialist: str
) -> None:
    from openexecutive.evals.scenarios import _load_builtin_scenarios

    monkeypatch.delenv("EVAL_SCENARIOS_PATH", raising=False)
    by_id = {s["id"]: s for s in _load_builtin_scenarios()}
    scenario = by_id[scenario_id]

    assert scenario["_kind"] == "chat"
    assert scenario["required_routing"] == [specialist]
    assert set(scenario["required_routing"]) <= set(SPECIALIST_REGISTRY)
    assert scenario["expected_topics"]


def test_bootstrapped_eval_puts_cash_before_fundraising() -> None:
    """The chat judge scores expected_topics (it only reads quality_criteria
    for peer-memory scenarios), so the cash-first intent must live there."""
    from openexecutive.evals.scenarios import _load_builtin_scenarios

    scenario = next(s for s in _load_builtin_scenarios() if s["id"] == "finance_bootstrapped_001")
    assert "bootstrapped" in scenario["company_context"]["stage"].lower()
    topics = " ".join(scenario["expected_topics"])
    assert "runway_at_current_burn" in topics
    assert "fundraising_only_if_the_founder_wants_it" in topics
    assert scenario["quality_criteria"]["does_not_default_to_fundraising"] is True
