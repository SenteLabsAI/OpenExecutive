from __future__ import annotations

import pytest

from openexecutive.architecture.facts import FactsBundle, gather_facts
from openexecutive.architecture.sections import SECTIONS, get_section


def test_sections_have_unique_stable_ids() -> None:
    ids = [s.id for s in SECTIONS]
    assert len(ids) == len(set(ids)), "section ids must be unique"
    # Lock in the canonical sections so the cache schema and UI nav stay
    # in lockstep with this registry. New sections MUST land here in the
    # same commit that adds them to SECTIONS — otherwise a section appears
    # in the nav with no docs page, or vice versa. The three subsystem
    # sections (org, scheduler, today) were added in PR #154.
    assert ids == [
        # "Without the Jargon" plain-language landing cluster, prepended
        # in PR #244 — non-engineer on-ramp; technical sections follow.
        "nojargon_what_it_is",
        "nojargon_authority",
        "nojargon_proactive",
        "nojargon_org",
        "overview",
        "lifecycle",
        "agents",
        "caching",
        "rag",
        "review",
        "memory",
        "peer_memory",
        "org",
        "audit",
        "schemas",
        "workflows",
        "scheduler",
        "integrations",
        # `external_monitoring` was added in the PR introducing the
        # external-condition monitoring layer — surfaces the watchlist
        # + adapters + research subsection from architecture-facts.yaml.
        "external_monitoring",
        "today",
        "api",
        # `mcp_server` — Open Executive exposed as an MCP server (the inverse
        # of the MCP gateway). New top-level module under packages/core.
        "mcp_server",
        # `user_guide` — documents the /guide user-guide surface (plain-language
        # per-feature overviews). New top-level module under packages/core.
        "user_guide",
        # `talent` — the talent-intelligence / executive-search vertical
        # (clients/engagements/candidates, matching graph, recruiting workflows,
        # /talent UI). New top-level module under packages/core.
        "talent",
        # `staff_onboarding` — onboarding a person INTO the company (templates,
        # per-hire plans + task checklists, role_onboarding brief workflow, ramp
        # drip, chat + /today integration). New top-level module.
        "staff_onboarding",
        # `clients` — named client-company slots for fractional / multi-client
        # use (save-back switching, per-client MCP config). New top-level module.
        "clients",
        # `security` — optional Grimdall execution guardrails on the tool-use
        # boundary (secret denial, egress allowlist, destructive block,
        # per-session spend budget). New top-level module under packages/core.
        "security",
    ]


def test_get_section_unknown_raises() -> None:
    with pytest.raises(KeyError, match="does-not-exist"):
        get_section("does-not-exist")


def test_facts_hash_is_deterministic() -> None:
    """Same inputs → same hash. Guarantees the cache key is stable."""
    a = FactsBundle(
        curated={"system": {"name": "Open Executive"}},
        agents=[],
        workflows=[],
        api_endpoints=[],
        health={"x": 1},
        code_excerpts=[],
        kb_chunks={"overview": "chunk-a"},
    )
    b = FactsBundle(
        curated={"system": {"name": "Open Executive"}},
        agents=[],
        workflows=[],
        api_endpoints=[],
        health={"x": 1},
        code_excerpts=[],
        kb_chunks={"overview": "chunk-a"},
    )
    assert a.core_hash() == b.core_hash()
    assert a.effective_hash_for_section("overview") == b.effective_hash_for_section("overview")


def test_facts_hash_changes_when_facts_change() -> None:
    """A real change to a cache-key input (curated content, agents,
    workflows, api_endpoints, code_excerpts) must produce a different
    `core_hash`. KB drift still changes `effective_hash_for_section`
    but not `core_hash` (see `test_kb_drift_does_not_invalidate_cache_key`).
    `health` and `curated_mtime` drift are explicitly excluded — see
    `test_health_drift_does_not_invalidate_cache_key` and
    `test_curated_mtime_drift_does_not_invalidate_cache_key`."""
    base = FactsBundle(
        curated={"x": 1},
        agents=[],
        workflows=[],
        api_endpoints=[],
        health={},
        code_excerpts=[],
        kb_chunks={"overview": "chunk"},
    )
    base_hash = base.effective_hash_for_section("overview")

    # Changed curated facts → core_hash diverges.
    drifted = base.model_copy(update={"curated": {"x": 2}})
    assert drifted.core_hash() != base.core_hash()
    assert drifted.effective_hash_for_section("overview") != base_hash

    # Changed KB slice → effective hash diverges even though core is identical.
    kb_drift = base.model_copy(update={"kb_chunks": {"overview": "different"}})
    assert kb_drift.core_hash() == base.core_hash()
    assert kb_drift.effective_hash_for_section("overview") != base_hash


def test_kb_drift_does_not_invalidate_cache_key() -> None:
    """KB chunks ground the LLM at generation time but must NOT affect
    `core_hash` (the live cache key). ChromaDB's approximate NN returns
    can wobble per request, and folding them in caused every page view
    to be a cache miss in production."""
    bundle = FactsBundle(
        curated={},
        agents=[],
        workflows=[],
        api_endpoints=[],
        health={},
        code_excerpts=[],
        kb_chunks={"overview": "A", "agents": "B"},
    )
    drifted = bundle.model_copy(
        update={"kb_chunks": {"overview": "A-WOBBLED", "agents": "B-WOBBLED"}}
    )
    assert bundle.core_hash() == drifted.core_hash()


def test_curated_mtime_drift_does_not_invalidate_cache_key() -> None:
    """File mtime is a process-local freshness signal only — it must
    NOT be folded into `core_hash`. File mtimes reset on every Docker
    `COPY` and every `git clone`, so including the mtime would (and
    historically did) bust the persistent section cache on every
    deploy even when no code or YAML had changed. An actual YAML edit
    invalidates the cache via the `curated` content hash; the mtime
    is incidental."""
    base = FactsBundle(
        curated={"x": 1},
        curated_mtime=1.0,
        agents=[],
        workflows=[],
        api_endpoints=[],
        health={},
        code_excerpts=[],
        kb_chunks={"overview": "chunk"},
    )
    mtime_drift = base.model_copy(update={"curated_mtime": 9999.9})
    assert base.core_hash() == mtime_drift.core_hash()

    # Sanity check the inverse: a real content change still busts the cache.
    content_drift = base.model_copy(update={"curated": {"x": 2}})
    assert base.core_hash() != content_drift.core_hash()


def test_health_drift_does_not_invalidate_cache_key() -> None:
    """`health` carries observability data (KB chunk count, profile
    flags) that drifts mid-process — KB ingestion happens after
    startup, the user fills in their profile, etc. Folding it into
    the cache key would regenerate every section on each of those
    unrelated events. It stays on the bundle (the debug endpoint and
    the generator's grounding prompt both consume it), but core_hash
    must ignore it."""
    base = FactsBundle(
        curated={},
        agents=[],
        workflows=[],
        api_endpoints=[],
        health={"builtin_knowledge_chunks": 100, "company_name": None},
        code_excerpts=[],
        kb_chunks={},
    )
    drifted = base.model_copy(
        update={
            "health": {
                "builtin_knowledge_chunks": 250,
                "company_name": "Acme",
                "company_profile_loaded": True,
            }
        }
    )
    assert base.core_hash() == drifted.core_hash()


def test_gather_facts_core_hash_is_idempotent() -> None:
    """Two back-to-back calls to `gather_facts` must produce equal
    `core_hash` values. A regression here means some new non-deterministic
    input has slipped into `_component_hashes` — exactly the failure
    mode that broke the persistent section cache for months."""
    a = gather_facts(include_kb=False)
    b = gather_facts(include_kb=False)
    assert a.core_hash() == b.core_hash()


def test_gather_facts_runs_without_kb() -> None:
    """Smoke-test the assembly path with KB disabled (no ChromaDB needed)."""
    bundle = gather_facts(include_kb=False)
    # Pulled from the live registry — at minimum, the eight specialists + triage.
    assert len(bundle.agents) >= 8
    # Workflows registry is non-empty.
    assert len(bundle.workflows) >= 1
    # OpenAPI walk picks up real endpoints.
    assert any(e.path.startswith("/architecture") for e in bundle.api_endpoints), (
        "expected the new /architecture endpoints to be discoverable via OpenAPI"
    )
    # Curated facts loaded.
    assert "system" in bundle.curated
    # Hash is reproducible.
    assert bundle.core_hash() == bundle.core_hash()
