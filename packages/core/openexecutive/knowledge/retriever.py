from __future__ import annotations

import re
from typing import Any

from openexecutive.audit import get_active_ids
from openexecutive.audit import log_event as _audit_log
from openexecutive.knowledge.review_store import (
    PRIORITY_ORDER,
    ContentType,
    Priority,
    ReviewStore,
)
from openexecutive.knowledge.store import ChromaDBStore

# Cosine distance threshold for the main retrieve() path. Hits with a
# distance > this are dropped before the top-K slice. Mirrors the value
# already used by retrieve_failures() — weak matches are noise that
# poisons grounding (e.g. a "Hi" greeting pulling a GitLab handbook
# chunk because it happens to be the closest seeded knowledge).
_DISTANCE_THRESHOLD = 0.55

# Minimum character length for RAG to fire. Below this we treat the
# message as a greeting / acknowledgement ("Hi", "ok") and skip the
# vector store entirely. Char count (not token count) because `\w+`
# matches a CJK sentence as a single token, which would incorrectly
# bypass RAG for meaningful Chinese/Japanese queries. Threshold sits
# at 3 so 3-letter business acronyms ("ROI", "CFO", "P&L") still fire.
_MIN_QUERY_CHARS = 3


_ATX_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")


def _neutralize_rag_headings(text: str) -> str:
    """Strip ATX headings so untrusted wiki text cannot spoof RAG section labels."""
    return _ATX_HEADING.sub("", text)


def _format_untrusted_wiki(text: str) -> str:
    """Prefix every line so wiki prose cannot impersonate citation markers."""
    cleaned = _neutralize_rag_headings(text)
    return "\n".join(f"· {line}" for line in cleaned.splitlines())


def _passes_threshold(
    row: dict[str, Any], threshold: float = _DISTANCE_THRESHOLD
) -> bool:
    """True iff the Chroma row's cosine distance is within the relevance gate.

    Treats a missing/None distance as out-of-bounds (we don't surface chunks
    of unknown relevance). Uses an explicit None check rather than `... or
    1.0` because `0.0 or 1.0 == 1.0` would falsy-drop the strongest possible
    match — Chroma returns 0.0 for a verbatim hit. ``threshold`` defaults to
    the module constant but callers pass the settings-configured value.
    """
    distance = row.get("distance")
    if distance is None:
        return False
    return distance <= threshold


def _default_review_store() -> ReviewStore:
    from openexecutive.memory.episodic import DB_PATH

    return ReviewStore(db_path=DB_PATH)


def _dedupe_by_text(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate-text hits from a Chroma result list.

    Multi-domain OER sources fan each chunk out to one row per declared
    domain. A specialist query that filters by domain naturally gets one
    row per chunk, but an unfiltered call (e.g. the Executive's global
    retrieve) could see the same passage 2-5x. Preserve order so the most
    semantically relevant copy wins.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in results:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        out.append(r)
    return out


def _emit_retrieval_audit(
    *,
    query: str,
    domain_filter: list[str] | None,
    specialist_name: str | None,
    builtin_results: list[dict[str, Any]],
    company_results: list[dict[str, Any]],
    annotation_count: int,
    collection: str,
) -> None:
    """Fire-and-forget audit emit for a retrieval pass.

    Reads (session_id, turn_id) from the audit context vars set by the
    Executive at turn entry; emits None for both when called outside a
    turn (CLI, ad-hoc workflows) so the row is still captured but won't
    cluster into a session timeline. log_event already swallows.
    """
    session_id, turn_id = get_active_ids()

    def _chunks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "source": r.get("metadata", {}).get("filename"),
                "domain": r.get("metadata", {}).get("domain"),
                "distance": r.get("distance"),
                # First 400 chars is enough to recognise the passage in the
                # UI without bloating audit rows; full text lives in Chroma.
                "text_preview": (r.get("text") or "")[:400],
            }
            for r in rows
        ]

    total = len(builtin_results) + len(company_results)
    domain_str = ",".join(domain_filter) if domain_filter else "*"
    _audit_log(
        "knowledge_retrieval",
        f"retrieve({domain_str}) → {total} chunks: {query[:140]}",
        session_id=session_id,
        turn_id=turn_id,
        actor=specialist_name or "executive",
        details={
            "query": query[:300],
            "collection": collection,
            "domain_filter": domain_filter,
            "specialist": specialist_name,
            "builtin_count": len(builtin_results),
            "company_count": len(company_results),
            "annotation_count": annotation_count,
        },
        full={
            "query": query,
            "domain_filter": domain_filter,
            "specialist": specialist_name,
            "builtin_chunks": _chunks(builtin_results),
            "company_chunks": _chunks(company_results),
        },
    )


DOMAIN_ALIASES: dict[str, list[str]] = {
    "cso": ["strategy"],
    "cfo": ["finance"],
    "chro": ["hr"],
    "gc": ["legal"],
    "coo": ["operations"],
    "cmo": ["marketing"],
    "cpo": ["product", "strategy"],
    "board_comms": ["board", "finance"],
    # The talent specialist reuses the existing HR + strategy knowledge
    # domains until a dedicated `talent` knowledge corpus is seeded (Phase 2).
    "talent": ["hr", "strategy"],
}


def retrieve(
    query: str,
    domain_filter: list[str] | None = None,
    specialist_name: str | None = None,
    n_builtin: int | None = None,
    n_company: int | None = None,
    store: ChromaDBStore | None = None,
    review_store: ReviewStore | None = None,
    distance_threshold: float | None = None,
) -> str:
    effective_domains = domain_filter
    if effective_domains is None and specialist_name:
        effective_domains = DOMAIN_ALIASES.get(specialist_name)

    # Short-message bypass: greetings and acknowledgements never benefit
    # from semantic retrieval and reliably surface noise. Skip the ChromaDB
    # roundtrip entirely, but still emit audit so the flow chart records
    # "we considered RAG and gated it out". Longer-but-tangential queries
    # are caught by the distance threshold below, not here.
    if len(query.strip()) < _MIN_QUERY_CHARS:
        _emit_retrieval_audit(
            query=query,
            domain_filter=effective_domains,
            specialist_name=specialist_name,
            builtin_results=[],
            company_results=[],
            annotation_count=0,
            collection="builtin+company (bypassed: short query)",
        )
        return ""

    # Resolve tunable retrieval params from settings when not explicitly
    # passed. Callers that pass values (e.g. the report workflows) keep
    # them; the chat path leaves them None and inherits the configured
    # defaults. get_settings() is uncached, so KNOWLEDGE_* env overrides
    # take effect on the next call — this is the lever the RAG ablation
    # harness toggles (KNOWLEDGE_BUILTIN_N_RESULTS=0 disables builtin RAG).
    from openexecutive.config import get_settings

    settings = get_settings()
    if n_builtin is None:
        n_builtin = settings.knowledge_builtin_n_results
    if n_company is None:
        n_company = settings.knowledge_company_n_results
    if distance_threshold is None:
        distance_threshold = settings.knowledge_distance_threshold

    if store is None:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)

    rs = review_store or _default_review_store()
    rejected_builtin = rs.get_rejected_filenames(ContentType.BUILTIN)
    rejected_external = rs.get_rejected_source_ids()
    priority_map = rs.get_priority_map(ContentType.BUILTIN)

    # Over-fetch slightly so post-query text dedup (multi-domain chunks share
    # the same text across rows) still leaves us with the requested count.
    # n_builtin <= 0 disables builtin-knowledge retrieval entirely (the
    # lever the RAG ablation harness flips). Skip the query rather than
    # asking Chroma for 0 results.
    if n_builtin > 0:
        raw_builtin = _dedupe_by_text(
            store.query(
                query_text=query,
                collection=ChromaDBStore.BUILTIN_COLLECTION,
                domain_filter=effective_domains,
                n_results=n_builtin * 3,
            )
        )

        # Filter out rejected files and rejected OER sources, drop weak
        # matches, then sort by SME priority.
        filtered_builtin = [
            r
            for r in raw_builtin
            if r["metadata"].get("filename") not in rejected_builtin
            and r["metadata"].get("source_id") not in rejected_external
            and _passes_threshold(r, distance_threshold)
        ]
        filtered_builtin.sort(
            key=lambda r: PRIORITY_ORDER.get(
                priority_map.get(r["metadata"].get("filename", ""), Priority.NORMAL.value),
                1,
            )
        )
        builtin_results = filtered_builtin[:n_builtin]
    else:
        builtin_results = []

    if n_company > 0:
        raw_company = store.query(
            query_text=query,
            collection=ChromaDBStore.COMPANY_COLLECTION,
            domain_filter=effective_domains,
            n_results=n_company,
        )
        company_results = [
            r for r in raw_company if _passes_threshold(r, distance_threshold)
        ]
    else:
        company_results = []

    # Synced Notion wiki — isolated from COMPANY because a Notion share is
    # multi-writer and unreviewed. Ranked below curated company docs and
    # labelled so specialists do not treat it as policy.
    raw_notion = store.query(
        query_text=query,
        collection=ChromaDBStore.NOTION_COLLECTION,
        domain_filter=effective_domains,
        n_results=3,
    )
    notion_results = [
        r for r in raw_notion if _passes_threshold(r, distance_threshold)
    ]

    # Recent research artifacts — kept in a separate collection and ranked
    # BELOW curated company docs. These are unvetted, web-sourced summaries
    # from executive_research runs, so they are clearly labelled as such and
    # never blended into the company-documents section above.
    raw_research = store.query(
        query_text=query,
        collection=ChromaDBStore.RESEARCH_COLLECTION,
        domain_filter=None,  # research is cross-domain; never domain-scoped
        n_results=2,
    )
    research_results = [
        r for r in raw_research if _passes_threshold(r, distance_threshold)
    ]

    active_annotations = rs.list_annotations(domains=effective_domains, active_only=True)

    # Audit emit — always fire, even on empty results, so the flow chart
    # can show "we asked but found nothing" rather than silently omitting
    # the retrieval step. Fire-and-forget; never blocks/breaks the caller.
    _emit_retrieval_audit(
        query=query,
        domain_filter=effective_domains,
        specialist_name=specialist_name,
        builtin_results=builtin_results,
        company_results=company_results,
        annotation_count=len(active_annotations),
        collection="builtin+company",
    )

    if (
        not builtin_results
        and not company_results
        and not notion_results
        and not research_results
        and not active_annotations
    ):
        return ""

    parts: list[str] = []

    if company_results:
        parts.append("### From your company documents:")
        for r in company_results:
            filename = r["metadata"].get("filename", "unknown")
            parts.append(f"[{filename}] {r['text']}")

    if notion_results:
        parts.append(
            "### Synced Notion wiki (unreviewed, multi-writer — weigh below "
            "curated company documents):"
        )
        for r in notion_results:
            filename = r["metadata"].get("filename", "unknown")
            parts.append(
                f"[notion:{filename}]\n{_format_untrusted_wiki(r['text'])}"
            )

    if research_results:
        parts.append(
            "### Recent research (unverified, web-sourced — weigh below "
            "company documents):"
        )
        for r in research_results:
            created = r["metadata"].get("created_at", "")
            when = f" — {created}" if created else ""
            parts.append(f"[recent research{when}] {r['text']}")

    if builtin_results:
        parts.append("### From executive knowledge base:")
        for r in builtin_results:
            filename = r["metadata"].get("filename", "unknown")
            prio = priority_map.get(filename, Priority.NORMAL.value)
            prefix = "[verified - priority source] " if prio == Priority.HIGH.value else ""
            parts.append(f"[{filename}] {prefix}{r['text']}")

    if active_annotations:
        parts.append("### SME corrections and context:")
        for ann in active_annotations:
            parts.append(f"[SME annotation] {ann.correction}")

    return "\n\n".join(parts)


def retrieve_failures(
    query: str,
    domain_filter: list[str] | None = None,
    specialist_name: str | None = None,
    n_results: int = 2,
    store: ChromaDBStore | None = None,
) -> str:
    """Query the failure_cases collection and return formatted context.

    Returns an empty string if no result clears the distance threshold —
    tangential failure stories are noise, so we prefer surfacing nothing
    over surfacing a poor match.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    if store is None:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)

    effective_domains = domain_filter
    if effective_domains is None and specialist_name:
        effective_domains = DOMAIN_ALIASES.get(specialist_name)

    raw = store.query(
        query_text=query,
        collection=ChromaDBStore.FAILURES_COLLECTION,
        domain_filter=effective_domains,
        n_results=n_results * 2,
    )

    # Cosine distance threshold (configurable via KNOWLEDGE_DISTANCE_THRESHOLD):
    # a larger distance means the match is too weak to be useful.
    threshold = settings.knowledge_distance_threshold
    filtered = _dedupe_by_text([r for r in raw if r["distance"] <= threshold])
    results = filtered[:n_results]

    # Audit emit (failure cases collection). Fire even when empty so the
    # timeline shows we considered failure stories and rejected them.
    _emit_retrieval_audit(
        query=query,
        domain_filter=effective_domains,
        specialist_name=specialist_name,
        builtin_results=results,
        company_results=[],
        annotation_count=0,
        collection="failure_cases",
    )

    if not results:
        return ""

    parts = ["### Relevant failure cases:"]
    for r in results:
        filename = r["metadata"].get("filename", "unknown")
        parts.append(f"[{filename}] {r['text']}")
    return "\n\n".join(parts)
