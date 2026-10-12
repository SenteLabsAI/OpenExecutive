"""Working agreements between a partner seat and the human it supports.

One row per seat. The row moves ``not_started`` → ``in_progress`` (interview
under way) → ``active`` (the human confirmed the read-back), or ``deferred``
(the human said not now — the Executive stops offering until asked). The
agreement text is injected into that seat's context on every consult, so what
the person said about how they want to work actually changes the seat's
behavior. Stored in the shared episodic SQLite file; created lazily.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

import openexecutive.memory.episodic as _episodic

logger = logging.getLogger(__name__)

AgreementStatus = Literal["not_started", "in_progress", "active", "deferred"]

#: Seats that partner with a human. Owner seats (cfo/coo) have no human to interview.
PARTNER_SEATS: tuple[str, ...] = ("cmo", "cpo", "ceo_advisor")

MAX_AGREEMENT_CHARS = 6000
MAX_QUESTIONS = 12
DEFAULT_REVIEW_DAYS = 30

_DDL = """
CREATE TABLE IF NOT EXISTS partner_agreements (
    seat TEXT PRIMARY KEY,
    person_id INTEGER,
    status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (status IN ('not_started','in_progress','active','deferred')),
    agreement_md TEXT NOT NULL DEFAULT '',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    agreed_at TEXT,
    review_after TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)
"""


class Agreement(BaseModel):
    seat: str
    person_id: int | None = None
    status: AgreementStatus = "not_started"
    agreement_md: str = ""
    open_questions: list[str] = Field(default_factory=list)
    started_at: str | None = None
    agreed_at: str | None = None
    review_after: str | None = None
    version: int = 0
    updated_at: str = ""

    def review_due(self, now: datetime | None = None) -> bool:
        if self.status != "active" or not self.review_after:
            return False
        try:
            due = datetime.fromisoformat(self.review_after)
        except ValueError:
            return False
        return (now or datetime.now(UTC)) >= due


def _path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else _episodic.DB_PATH


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(db_path: Path | None, *, create: bool) -> sqlite3.Connection | None:
    path = _path(db_path)
    if not create and not path.exists():
        return None  # reads never create a database file as a side effect
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    return conn


def _row(row: sqlite3.Row) -> Agreement:
    try:
        questions = [str(q) for q in json.loads(row["open_questions_json"] or "[]")]
    except (ValueError, TypeError):
        questions = []
    return Agreement(
        seat=row["seat"],
        person_id=row["person_id"],
        status=row["status"],
        agreement_md=row["agreement_md"] or "",
        open_questions=questions,
        started_at=row["started_at"],
        agreed_at=row["agreed_at"],
        review_after=row["review_after"],
        version=row["version"],
        updated_at=row["updated_at"],
    )


def get_agreement(seat: str, db_path: Path | None = None) -> Agreement | None:
    conn = _connect(db_path, create=False)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM partner_agreements WHERE seat = ?", (seat,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def begin_introduction(seat: str, person_id: int, db_path: Path | None = None) -> Agreement:
    """Mark the interview as under way. An ``active`` agreement stays active (review mode)."""
    now = _now()
    conn = _connect(db_path, create=True)
    assert conn is not None
    try:
        existing = conn.execute("SELECT status FROM partner_agreements WHERE seat = ?", (seat,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO partner_agreements (seat, person_id, status, started_at, updated_at) "
                "VALUES (?, ?, 'in_progress', ?, ?)",
                (seat, person_id, now, now),
            )
        elif existing["status"] != "active":
            conn.execute(
                "UPDATE partner_agreements SET status='in_progress', person_id=?, started_at=?, "
                "updated_at=? WHERE seat=?",
                (person_id, now, now, seat),
            )
        else:
            conn.execute(
                "UPDATE partner_agreements SET started_at=?, updated_at=? WHERE seat=?",
                (now, now, seat),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM partner_agreements WHERE seat = ?", (seat,)).fetchone()
        return _row(row)
    finally:
        conn.close()


def save_agreement(
    seat: str,
    person_id: int,
    *,
    agreement_md: str,
    open_questions: list[str],
    status: Literal["active", "deferred"] = "active",
    review_in_days: int = DEFAULT_REVIEW_DAYS,
    db_path: Path | None = None,
) -> Agreement:
    """Persist the confirmed agreement (or a deferral) and bump the version."""
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    questions = [q.strip()[:300] for q in open_questions if q and q.strip()][:MAX_QUESTIONS]
    review_after = (now_dt + timedelta(days=review_in_days)).isoformat() if status == "active" else None
    conn = _connect(db_path, create=True)
    assert conn is not None
    try:
        prior = conn.execute("SELECT * FROM partner_agreements WHERE seat = ?", (seat,)).fetchone()
        if status == "deferred" and prior is not None and prior["status"] == "active":
            # A deferral never discards an agreement that was already confirmed.
            return _row(prior)
        version = (prior["version"] if prior else 0) + (1 if status == "active" else 0)
        started = prior["started_at"] if prior and prior["started_at"] else now
        conn.execute(
            "INSERT INTO partner_agreements "
            "(seat, person_id, status, agreement_md, open_questions_json, started_at, agreed_at, "
            " review_after, version, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(seat) DO UPDATE SET person_id=excluded.person_id, status=excluded.status, "
            "agreement_md=CASE WHEN excluded.status='active' THEN excluded.agreement_md "
            "ELSE partner_agreements.agreement_md END, "
            "open_questions_json=excluded.open_questions_json, started_at=excluded.started_at, "
            "agreed_at=CASE WHEN excluded.status='active' THEN excluded.agreed_at "
            "ELSE partner_agreements.agreed_at END, review_after=excluded.review_after, "
            "version=excluded.version, updated_at=excluded.updated_at",
            (
                seat,
                person_id,
                status,
                agreement_md[:MAX_AGREEMENT_CHARS] if status == "active" else "",
                json.dumps(questions),
                started,
                now if status == "active" else None,
                review_after,
                version,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM partner_agreements WHERE seat = ?", (seat,)).fetchone()
        return _row(row)
    finally:
        conn.close()


def list_agreements(db_path: Path | None = None) -> list[Agreement]:
    conn = _connect(db_path, create=False)
    if conn is None:
        return []
    try:
        return [_row(r) for r in conn.execute("SELECT * FROM partner_agreements ORDER BY seat")]
    finally:
        conn.close()


def agreement_context(seat: str, db_path: Path | None = None) -> str:
    """The ``<working_agreement>`` block injected into a seat's context ('' if none).

    Never raises: a store problem must not break a specialist consult.
    """
    try:
        agreement = get_agreement(seat, db_path=db_path)
    except Exception:  # noqa: BLE001
        logger.warning("partners: could not read agreement for %s", seat, exc_info=True)
        return ""
    if agreement is None or agreement.status != "active" or not agreement.agreement_md.strip():
        return ""
    parts = [
        "<working_agreement>",
        f"How the person you partner with has asked to work with you (confirmed {agreement.agreed_at[:10] if agreement.agreed_at else 'earlier'}, v{agreement.version}). "
        "This overrides your default style, cadence and level of initiative. It never overrides the "
        "decision priority stack, data discipline, or the rule that the human decides.",
        agreement.agreement_md.strip(),
    ]
    if agreement.open_questions:
        parts.append(
            "Still to learn (ask when it is natural, one at a time):\n"
            + "\n".join(f"- {q}" for q in agreement.open_questions)
        )
    if agreement.review_due():
        parts.append(
            "Review due: this agreement is past its review date. Offer a short check-in on whether "
            "it still fits before relying on it."
        )
    parts.append("</working_agreement>")
    return "\n".join(parts)
