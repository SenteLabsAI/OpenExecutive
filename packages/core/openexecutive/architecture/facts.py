"""Gather the four ground-truth inputs the architecture generator needs.

The bundle is hashed (SHA-256 of canonical JSON) so the cache layer can
detect when *any* fact has changed and force regeneration of the
affected sections. Anything we put in the bundle becomes a cache-bust
trigger, so we keep it bounded: code excerpts are truncated, the
OpenAPI spec is summarised, KB hits are limited.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_FACTS_YAML = _HERE / "architecture-facts.yaml"
_PKG_ROOT = _HERE.parent  # openexecutive/

# Key files whose first N lines are bundled as repo-truth excerpts. The
# list is deliberately small: every entry is a fact source that breaks
# the cache when its head shifts. Keep additions narrow to the public
# surface of the architecture.
_CODE_EXCERPT_FILES: list[tuple[str, Path, int]] = [
    ("orchestrator/router.py", _PKG_ROOT / "orchestrator" / "router.py", 100),
    ("orchestrator/executive.py", _PKG_ROOT / "orchestrator" / "executive.py", 80),
    ("prompts/cache_manager.py", _PKG_ROOT / "prompts" / "cache_manager.py", 120),
    ("knowledge/retriever.py", _PKG_ROOT / "knowledge" / "retriever.py", 70),
    ("memory/episodic.py", _PKG_ROOT / "memory" / "episodic.py", 80),
    ("audit/logger.py", _PKG_ROOT / "audit" / "logger.py", 100),
]


class AgentFact(BaseModel):
    name: str
    role: str
    model: str
    deep_reasoning: bool
    domains: list[str]


class WorkflowFact(BaseModel):
    name: str
    title: str
    description: str
    # Read defensively: WorkflowMeta has been renamed at least once
    # (`category` → `section`), and a future rename should not 500 the
    # entire /architecture page. We coerce whatever the live schema
    # exposes into a plain string label.
    section: str = ""
    steps: list[dict[str, str]] = Field(default_factory=list)


class ApiEndpointFact(BaseModel):
    method: str
    path: str
    summary: str
    tags: list[str] = Field(default_factory=list)


class CodeExcerpt(BaseModel):
    path: str
    head: str  # First N lines of the file


class FactsBundle(BaseModel):
    """The complete, hashable bundle of facts fed to the generator.

    Two different runs producing the same bundle (by canonical JSON
    serialisation) hash identically — that's the cache key.
    """

    curated: dict[str, Any]
    # In-process freshness signal only. Used by `_bundle_is_fresh` in
    # `api/routes/architecture.py` to detect external YAML edits without
    # waiting out the 30s bundle TTL. NOT folded into `core_hash`: file
    # mtimes are reset on every Docker `COPY` and every `git clone`, so
    # including this in the cache key would (and historically did) bust
    # the persistent section cache on every deploy. The `curated` content
    # itself is what invalidates the cache when the YAML actually changes.
    curated_mtime: float = 0.0
    agents: list[AgentFact]
    workflows: list[WorkflowFact]
    api_endpoints: list[ApiEndpointFact]
    # Observability snapshot (KB chunk count, profile-loaded flag). Held
    # for the `/architecture/debug/bundle` endpoint and as grounding
    # context for the generator, but NOT in the cache key: these values
    # drift mid-process (KB ingestion, profile edits) and would otherwise
    # trigger spurious regenerations for unrelated state changes.
    health: dict[str, Any]
    code_excerpts: list[CodeExcerpt]
    # Per-section KB context grounds the LLM at generation time but is
    # NOT part of the cache key. ChromaDB's approximate nearest-neighbour
    # can return slightly different top-K on each call (tie boundary
    # shifts), so hashing it caused every page view to be a cache miss.
    kb_chunks: dict[str, str] = Field(default_factory=dict)

    def _component_hashes(self) -> dict[str, str]:
        """SHA-256 of each input independently. Used to diagnose which
        input is drifting when the cache misses unexpectedly.

        Deliberately omits `curated_mtime`, `health`, and `kb_chunks`:
        each is non-deterministic across deploys or within a single
        process, and folding them in caused the persistent section
        cache to invalidate on every request. The remaining components
        all change ONLY when the underlying code or curated YAML
        actually changes — so this dict is the canonical set of
        cache-key inputs.
        """

        def h(value: Any) -> str:
            return hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                .encode("utf-8")
            ).hexdigest()

        return {
            "curated": h(self.curated),
            "agents": h([a.model_dump() for a in self.agents]),
            "workflows": h([w.model_dump() for w in self.workflows]),
            "api_endpoints": h([e.model_dump() for e in self.api_endpoints]),
            "code_excerpts": h([c.model_dump() for c in self.code_excerpts]),
        }

    def core_hash(self) -> str:
        """Hash of the cache-key components defined in `_component_hashes`.

        Deterministic across processes and deploys for the same git SHA
        and curated YAML content. The `curated` content itself is what
        invalidates the cache on a real YAML edit — file mtime is
        explicitly NOT in this hash (see the comment on `curated_mtime`).
        """
        components = self._component_hashes()
        canonical = json.dumps(components, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def effective_hash_for_section(self, section_id: str) -> str:
        """DEPRECATED. Kept for tests asserting hash properties.

        Previously the per-section cache key folded in KB chunks so that
        any drift in RAG output invalidated that section. In practice
        ChromaDB's approximate nearest-neighbour returns can shift
        slightly at the tie boundary on every query, so folding KB into
        the cache key meant *every* page view was a miss. Production
        now uses `core_hash()` directly — KB drift no longer
        invalidates, and a real corpus change still does (since chunk
        counts and the curated facts move with it).
        """
        section_kb = self.kb_chunks.get(section_id, "")
        material = self.core_hash() + "|" + section_kb
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_curated() -> dict[str, Any]:
    if not _FACTS_YAML.exists():
        return {}
    try:
        with open(_FACTS_YAML, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        # A corrupt or hand-edited YAML must not 500 the entire
        # /architecture endpoint. Log and fall back to empty curated
        # facts; downstream generation will still work, just with less
        # grounding context.
        logger.warning("facts.curated.parse_failed: %s", exc)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("facts.curated.unexpected_type: %r", type(loaded).__name__)
        return {}
    return loaded


def _curated_mtime() -> float:
    """File mtime of the curated facts YAML.

    Used ONLY as an in-process freshness signal by `_bundle_is_fresh`
    in `api/routes/architecture.py` — when the cached FactsBundle's
    captured mtime differs from the file's current mtime, the bundle
    is rebuilt before the 30s TTL would otherwise expire. NOT folded
    into `core_hash`: file mtimes reset on every Docker `COPY` and
    every `git clone`, which would (and historically did) bust the
    persistent section cache on every deploy. Real YAML edits
    invalidate the cache via the `curated` content hash instead.
    """
    try:
        return _FACTS_YAML.stat().st_mtime if _FACTS_YAML.exists() else 0.0
    except OSError:
        return 0.0


def _collect_agents() -> list[AgentFact]:
    from openexecutive.knowledge.retriever import DOMAIN_ALIASES
    from openexecutive.orchestrator.router import (
        SPECIALIST_DESCRIPTIONS,
        SPECIALIST_REGISTRY,
    )

    out: list[AgentFact] = []
    for name, agent in SPECIALIST_REGISTRY.items():
        desc = SPECIALIST_DESCRIPTIONS.get(name, name)
        role = desc.split(" — ")[0] if " — " in desc else desc
        out.append(
            AgentFact(
                name=name,
                role=role,
                model=agent.model,
                deep_reasoning=agent.use_deep_reasoning,
                domains=DOMAIN_ALIASES.get(name, []),
            )
        )
    return out


def _collect_workflows() -> list[WorkflowFact]:
    """Snapshot the workflow registry. Per-workflow failures are isolated
    so that one bad row can't 500 every architecture endpoint — this is
    a docs surface, not a critical path."""
    from openexecutive.workflows import list_workflows

    out: list[WorkflowFact] = []
    for w in list_workflows():
        try:
            meta = w.meta()
            steps = [
                {"id": s.id, "title": s.title, "description": s.description}
                for s in meta.steps
            ]
            # `section` is the current field on WorkflowMeta; older
            # builds called it `category`. Fall back across renames and
            # coerce StrEnum values to plain strings.
            section_val = getattr(meta, "section", None) or getattr(meta, "category", "")
            out.append(
                WorkflowFact(
                    name=meta.name,
                    title=meta.title,
                    description=meta.description,
                    section=str(section_val) if section_val else "",
                    steps=steps,
                )
            )
        except Exception as exc:
            logger.warning(
                "facts.workflow.skip name=%s: %s",
                getattr(w, "name", "?"), exc,
            )
            continue
    return out


def _collect_api_endpoints() -> list[ApiEndpointFact]:
    """Walk the OpenAPI spec without spinning up the lifespan."""
    from openexecutive.api.main import app

    spec = app.openapi()
    out: list[ApiEndpointFact] = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            out.append(
                ApiEndpointFact(
                    method=method.upper(),
                    path=path,
                    summary=str(op.get("summary") or "").strip(),
                    tags=list(op.get("tags") or []),
                )
            )
    out.sort(key=lambda e: (e.path, e.method))
    return out


def _collect_health() -> dict[str, Any]:
    """Best-effort health snapshot. Anything that throws is dropped."""
    snapshot: dict[str, Any] = {}
    try:
        from openexecutive.config import get_settings
        from openexecutive.knowledge.store import ChromaDBStore

        settings = get_settings()
        store = ChromaDBStore(persist_directory=settings.vector_store_path)
        snapshot["builtin_knowledge_chunks"] = store.get_collection_count(
            ChromaDBStore.BUILTIN_COLLECTION
        )
    except Exception as exc:
        logger.debug("facts.health.kb_skip: %s", exc)

    try:
        from openexecutive.onboarding.profile_builder import load_or_create_profile

        profile = load_or_create_profile()
        snapshot["company_profile_loaded"] = not profile.is_empty()
        snapshot["company_name"] = profile.name or None
    except Exception as exc:
        logger.debug("facts.health.profile_skip: %s", exc)

    return snapshot


def _collect_code_excerpts() -> list[CodeExcerpt]:
    out: list[CodeExcerpt] = []
    for rel, path, n_lines in _CODE_EXCERPT_FILES:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("facts.excerpt.read_skip path=%s: %s", path, exc)
            continue
        head = "\n".join(text.splitlines()[:n_lines])
        out.append(CodeExcerpt(path=rel, head=head))
    return out


def _collect_kb_chunks(specs: list[Any]) -> dict[str, str]:
    """Per-section KB hits. Failures are silenced — KB is best-effort
    grounding, not authoritative."""
    try:
        from openexecutive.knowledge.retriever import retrieve
    except Exception as exc:
        logger.debug("facts.kb.import_skip: %s", exc)
        return {}

    out: dict[str, str] = {}
    for spec in specs:
        try:
            out[spec.id] = retrieve(query=spec.kb_query, n_builtin=3, n_company=0)
        except Exception as exc:
            logger.debug("facts.kb.query_skip section=%s: %s", spec.id, exc)
            out[spec.id] = ""
    return out


def gather_facts(*, include_kb: bool = True) -> FactsBundle:
    """Assemble the full bundle. The only expensive piece is the KB
    fan-out — set `include_kb=False` in tests or smoke scripts that
    don't have ChromaDB available."""
    from openexecutive.architecture.sections import SECTIONS

    return FactsBundle(
        curated=_load_curated(),
        curated_mtime=_curated_mtime(),
        agents=_collect_agents(),
        workflows=_collect_workflows(),
        api_endpoints=_collect_api_endpoints(),
        health=_collect_health(),
        code_excerpts=_collect_code_excerpts(),
        kb_chunks=_collect_kb_chunks(SECTIONS) if include_kb else {},
    )
