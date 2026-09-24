"""On-disk cache for the on-page briefing narrative.

Mirrors `openexecutive.people.insights_cache`: a dedicated table in the
shared `./episodic_memory.db`, validated against an `input_hash` of the
briefing state. The narrative is one LLM call, so we never generate it on
the request hot path — `/today` serves the cached text instantly and, when
the state hash has moved, regenerates in a FastAPI background task (same
pattern as the per-person insight notes).

Keyed by a `scope` string rather than a person id: the principal and any
unrostered/unresolved viewer share the whole-company narrative under
`"principal"` (the default), while each non-principal teammate gets their own
role-scoped narrative under `person:<id>` (see `today._attach_narrative`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from openexecutive.memory import episodic as _episodic

logger = logging.getLogger(__name__)

# Explicit override slot. Left None in production so the cache follows the
# episodic DB dynamically (see _resolve_db_path) — same rationale as
# insights_cache: capturing episodic.DB_PATH at import time would make a
# test's monkeypatch of episodic.DB_PATH silently miss this table.
DB_PATH: Path | None = None

DEFAULT_SCOPE = "principal"


class BriefingNarrative(BaseModel):
    """A cached briefing narrative for one scope."""

    scope: str
    input_hash: str
    narrative_text: str
    generated_at: str  # ISO-8601 UTC


def _resolve_db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    if DB_PATH is not None:
        return DB_PATH
    return _episodic.DB_PATH


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    resolved = _resolve_db_path(db_path)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_narrative (
                scope TEXT PRIMARY KEY,
                input_hash TEXT NOT NULL,
                narrative_text TEXT NOT NULL,
                generated_at TEXT NOT NULL
            )
            """
        )


# Bump this whenever the narrative PROMPTS change (BRIEFING_NARRATIVE_SYSTEM /
# _viewer_system_prompt / STANDALONE_BRIEF_SYSTEM in narrative.py). It is folded
# into the input hash so a prompt change invalidates every cached narrative on
# the next view — otherwise a wording fix wouldn't surface until the underlying
# state changed (or the daily date rollover).
NARRATIVE_PROMPT_VERSION = "4"


# Stands in for the rendered context when nothing on the viewer's board wants
# a decision. The narrative is then a fixed line and no model call happens, so
# the key must NOT depend on anything volatile: hashing the real context there
# would re-key on every activity row and re-write the identical quiet line
# forever.
QUIET_CONTEXT = "<nothing-needs-attention>"


def build_narrative_input_hash(
    rendered_context: str, scope: str = DEFAULT_SCOPE
) -> str:
    """Hash of the EXACT user-turn context the model will be given.

    The one invariant that keeps this cache honest: **the key is a function of
    the model's input.** Anything the prompt renders is covered; anything it
    does not render cannot trigger a regeneration. Both properties are free,
    and they survive future edits to `render_briefing_context` without anyone
    remembering to update a field list here.

    Two earlier designs each broke one half of that, in opposite directions:

    * Keying on proposal headlines alone left the rendered activity block
      uncovered, so the header could describe a rail that had moved on.
    * Keying on the proposals' review fields (`review_verdict`, `review_note`,
      `why_now`, `recommended_move`, `due_at`) covered data the /today header
      never renders at all — it prints `headline[:160]` and nothing else — while
      `alerts.review` rewrites those fields with fresh LLM prose on EVERY pass,
      including its "still relevant, nothing changed" path. That regenerated
      every viewer's narrative several times a day to re-synthesize byte-
      identical input.

    ``scope`` keeps two viewers whose contexts coincide under distinct keys,
    ``NARRATIVE_PROMPT_VERSION`` invalidates every entry when a prompt is
    reworded, and the UTC date gives a daily floor (it is also inside the
    rendered context's PERIOD line, but the quiet sentinel has no such line).
    """
    if not isinstance(rendered_context, str):
        # This used to take the `today_data` dict. A dict is JSON-serialisable,
        # so a stale caller would hash silently and key the entry on something
        # the model never saw — a wrong cache that looks like a working one.
        raise TypeError(
            "build_narrative_input_hash takes the RENDERED context string "
            f"(see today._narrative_context), not {type(rendered_context).__name__}"
        )
    payload = {
        "scope": scope,
        "prompt_version": NARRATIVE_PROMPT_VERSION,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "context": rendered_context,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(scope: str = DEFAULT_SCOPE, db_path: Path | None = None) -> BriefingNarrative | None:
    initialize_db(db_path)
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT scope, input_hash, narrative_text, generated_at "
            "FROM briefing_narrative WHERE scope = ?",
            (scope,),
        ).fetchone()
    if row is None:
        return None
    return BriefingNarrative(
        scope=row["scope"],
        input_hash=row["input_hash"],
        narrative_text=row["narrative_text"],
        generated_at=row["generated_at"],
    )


def put(narrative: BriefingNarrative, db_path: Path | None = None) -> None:
    initialize_db(db_path)
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO briefing_narrative
              (scope, input_hash, narrative_text, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
              input_hash = excluded.input_hash,
              narrative_text = excluded.narrative_text,
              generated_at = excluded.generated_at
            """,
            (
                narrative.scope,
                narrative.input_hash,
                narrative.narrative_text,
                narrative.generated_at,
            ),
        )


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DEFAULT_SCOPE",
    "QUIET_CONTEXT",
    "BriefingNarrative",
    "build_narrative_input_hash",
    "get",
    "initialize_db",
    "put",
    "utc_now_iso",
]
