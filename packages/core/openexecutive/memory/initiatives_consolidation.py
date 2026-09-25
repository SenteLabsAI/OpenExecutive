"""One-time consolidation of duplicate initiative rows.

Background: the extraction LLM that populates `initiatives` historically wasn't
shown what already existed, so the same real-world project ("AI Opportunity
Assessment") accumulated as 5-10 near-duplicate rows under variant titles.
This module asks a routing-model pass to cluster the surviving duplicates and
optionally merges each cluster down to a single canonical row.

Designed as a one-off CLI operation, not an ongoing background task: future
duplicates are prevented at write time by the existing-initiatives block
appended to the extraction prompt in `episodic.extract_and_store`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory.episodic import (
    DB_PATH,
    Initiative,
    _get_conn,
    get_active_initiatives,
)

logger = logging.getLogger(__name__)


@dataclass
class Cluster:
    canonical_title: str
    member_ids: list[int]


_CLUSTER_TOOL: dict[str, Any] = {
    "name": "propose_clusters",
    "description": (
        "Cluster duplicate initiatives. Only cluster rows that clearly refer "
        "to the same real-world project. When in doubt, leave them separate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "description": "Each cluster groups initiatives that are the same real-world project. Omit singletons.",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical_title": {
                            "type": "string",
                            "description": "Best representative title for the cluster (3-6 words).",
                        },
                        "member_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Initiative row ids in this cluster (must contain at least 2).",
                        },
                    },
                    "required": ["canonical_title", "member_ids"],
                },
            },
        },
        "required": ["clusters"],
    },
}

_CLUSTER_SYSTEM = (
    "You are consolidating an episodic memory table where the same real-world "
    "initiative has been recorded multiple times under slightly different "
    "titles. Cluster ONLY initiatives that clearly describe the same "
    "underlying project. Distinct projects that happen to share vocabulary "
    "(e.g. two different marketing campaigns) MUST stay separate.\n\n"
    "Output rules:\n"
    "- Omit singletons. Only emit a cluster with 2+ member_ids.\n"
    "- The canonical_title should be the clearest, most specific title in the "
    "group, lightly cleaned (no edition labels like 'v2' or '$30K' unless "
    "they are core to the identity).\n"
    "- When uncertain, keep rows separate."
)


async def propose_clusters(
    db_path: Path = DB_PATH,
) -> tuple[list[Initiative], list[Cluster]]:
    """Ask the routing model to cluster active initiatives.

    Returns (all_initiatives, clusters). Empty cluster list means nothing
    worth merging.
    """
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.config import get_settings
    from openexecutive.providers import get_provider

    initiatives = get_active_initiatives(db_path=db_path)
    if len(initiatives) < 2:
        return initiatives, []

    listing = "\n".join(
        f"- id={i.id} title={i.title!r} summary={i.summary!r}"
        for i in initiatives
    )

    routing_model = get_settings().routing_model
    response = await get_provider(routing_model).messages_create(
        model=routing_model,
        max_tokens=2048,
        system=_CLUSTER_SYSTEM,
        tools=[_CLUSTER_TOOL],
        tool_choice={"type": "tool", "name": "propose_clusters"},
        messages=[
            {
                "role": "user",
                "content": f"ACTIVE INITIATIVES:\n{listing}",
            }
        ],
    )
    log_model_usage(response, model=routing_model, actor="initiatives_consolidation")

    clusters: list[Cluster] = []
    valid_ids = {i.id for i in initiatives}
    # Track ids already claimed by an emitted cluster so an LLM hallucination
    # of overlapping clusters (the schema doesn't forbid it) can't have two
    # different canonical titles fight over the same row.
    claimed_ids: set[int] = set()
    for block in response.content:
        if block.type != "tool_use" or block.name != "propose_clusters":
            continue
        for c in block.input.get("clusters", []):
            raw_ids = c.get("member_ids", []) or []
            ids: list[int] = []
            for x in raw_ids:
                # bool is a subclass of int — drop it explicitly so True/False
                # don't silently coerce to row ids 1/0.
                if x is None or isinstance(x, bool):
                    continue
                try:
                    parsed = int(x)
                except (TypeError, ValueError):
                    continue
                if parsed in valid_ids and parsed not in claimed_ids:
                    ids.append(parsed)
            # Drop bad clusters: singletons, all-unknown, all-already-claimed,
            # or empty title.
            title = (c.get("canonical_title") or "").strip()
            if len(ids) >= 2 and title:
                clusters.append(Cluster(canonical_title=title, member_ids=ids))
                claimed_ids.update(ids)

    return initiatives, clusters


def apply_clusters(
    clusters: list[Cluster], db_path: Path = DB_PATH
) -> dict[str, int]:
    """Merge each cluster down to one survivor row.

    Survivor = oldest created_at in the cluster (preserves provenance).
    Title is rewritten to the canonical title. Summary is the most-recent
    non-empty summary across cluster members. The other rows are deleted.

    Acquires an IMMEDIATE write lock so a concurrent background extraction
    pass (extract_and_store) cannot interleave with the merge — without
    this an extraction write between our SELECT and UPDATE/DELETE could be
    silently lost. The whole merge runs in one transaction.
    """
    now = datetime.now(UTC).isoformat()
    merged = 0
    deleted = 0

    with _get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for cluster in clusters:
            placeholders = ",".join(["?"] * len(cluster.member_ids))
            rows = conn.execute(
                f"SELECT id, title, summary, created_at, updated_at"  # noqa: S608 -- placeholder count is bounded by member_ids
                f" FROM initiatives WHERE id IN ({placeholders}) ORDER BY created_at",
                tuple(cluster.member_ids),
            ).fetchall()
            if len(rows) < 2:
                continue

            survivor = rows[0]
            best_summary = ""
            best_ts = ""
            for r in rows:
                if r["summary"] and r["updated_at"] > best_ts:
                    best_summary = r["summary"]
                    best_ts = r["updated_at"]

            conn.execute(
                "UPDATE initiatives SET title = ?, summary = ?, updated_at = ?"
                " WHERE id = ?",
                (
                    cluster.canonical_title,
                    best_summary or survivor["summary"],
                    now,
                    survivor["id"],
                ),
            )
            for r in rows[1:]:
                conn.execute("DELETE FROM initiatives WHERE id = ?", (r["id"],))
                deleted += 1
            merged += 1

    return {"clusters_merged": merged, "rows_deleted": deleted}
