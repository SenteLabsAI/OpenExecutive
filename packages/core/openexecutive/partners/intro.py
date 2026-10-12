"""The discovery interview a partner seat runs with the human it supports.

Three pieces, all deterministic (no model calls):

* ``owner_for_seat`` — which human a seat partners with, read from the
  department the seat serves (``DepartmentConfig.head_person_id``).
* ``build_knowledge_snapshot`` — an honest inventory of what the seat has
  consumed and what it cannot see yet, so the introduction opens with
  "here is what I know, correct me" instead of a blank questionnaire.
* ``build_interview_guide`` — the agenda: introduce, show the inventory, learn
  what it still needs, learn how the person wants to work, agree on first
  tasks, read the agreement back and get an explicit yes.

The Executive speaks it conversationally (see ``orchestrator/partner_tools.py``);
this module only supplies the substance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openexecutive.partners.store import PARTNER_SEATS, Agreement, get_agreement

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeatProfile:
    seat: str
    title: str  # how the seat is described to its human
    department_slug: str
    human_role: str
    asks: tuple[str, ...]  # things this seat specifically needs to learn


SEATS: dict[str, SeatProfile] = {
    "cmo": SeatProfile(
        seat="cmo",
        title="marketing partner",
        department_slug="marketing",
        human_role="CMO",
        asks=(
            "The brand foundation as it stands today, and which parts are settled versus still open.",
            "Who the audience is right now and what evidence you have about them.",
            "What has been tried in channels or messaging so far, and what you learned.",
            "Which claims the product can honestly keep today (so I can check copy against reality).",
            "What you would never want published, however good the numbers look.",
        ),
    ),
    "cpo": SeatProfile(
        seat="cpo",
        title="product partner (VP of Product)",
        department_slug="product",
        human_role="CPO",
        asks=(
            "The roadmap and what is deliberately off it, in your own words.",
            "Where discovery stands: what you have heard from real families and what is still guesswork.",
            "How the team uses its delivery tracker, so I read it the way you do.",
            "The product principles that are non-negotiable and where you feel pressure to bend them.",
            "Which decisions are open, and which you would like a second opinion on first.",
        ),
    ),
    "ceo_advisor": SeatProfile(
        seat="ceo_advisor",
        title="strategic partner",
        department_slug="strategy",
        human_role="CEO",
        asks=(
            "The three outcomes that must be true in 90 days for this to have been a good quarter.",
            "The decisions you are putting off and what is making them hard.",
            "What you want me to challenge hardest, and what you want me to leave alone.",
            "The financial and legal facts you consider fixed versus open.",
            "How you want to be told when I think you are wrong.",
        ),
    ),
}

_HOW_TO_WORK = (
    "How often and where you want to hear from me, and what should never interrupt you.",
    "Length and style: bottom line first, or the reasoning first? When do you want options versus a recommendation?",
    "When to challenge you versus just execute, and how blunt to be.",
    "What I may do without asking, what I must propose first, and what is off-limits entirely.",
    "What stays between us: anything you want kept out of what I share with the other partners or the weekly roll-up.",
    "What annoyed you about working with assistants, or with people in a role like mine, before.",
)


def owner_for_seat(seat: str, db_path: Path | None = None) -> Any | None:
    """The Person who heads the department this seat serves (None if unresolved)."""
    from openexecutive.departments import store as dept_store
    from openexecutive.people import store as people_store

    profile = SEATS.get(seat)
    if profile is None:
        return None
    candidates = [
        s
        for s in dept_store.list_departments(db_path=db_path)
        if s.config.specialist_key == seat or s.config.slug == profile.department_slug
    ]
    for state in candidates:
        if state.config.head_person_id is not None:
            return people_store.get_person(state.config.head_person_id, db_path=db_path)
    return None


def build_knowledge_snapshot(seat: str, db_path: Path | None = None) -> dict[str, Any]:
    """What the seat has to work with, and what it cannot see yet."""
    from openexecutive.departments import store as dept_store
    from openexecutive.feeds import FEEDS_BY_DEPARTMENT
    from openexecutive.knowledge.retriever import DOMAIN_ALIASES

    profile = SEATS[seat]
    state = dept_store.get_department(profile.department_slug, db_path=db_path)

    snapshot: dict[str, Any] = {
        "role": f"{profile.title} to the {profile.human_role}",
        "department": None,
        "goals": [],
        "live_feeds": [],
        "built_in_knowledge_domains": DOMAIN_ALIASES.get(seat, []),
        "not_visible_yet": [],
    }
    gaps: list[str] = snapshot["not_visible_yet"]

    if state is None:
        gaps.append(f"No {profile.department_slug} department record exists, so I have no charter or goals.")
    else:
        snapshot["department"] = {
            "title": state.config.title,
            "mission": state.config.charter.mission,
            "scope": state.config.charter.scope,
        }
        for goal in state.goals:
            snapshot["goals"].append(
                {
                    "goal": goal.key_result,
                    "target": goal.target,
                    "current": goal.current or None,
                    "status": goal.status,
                    "reviewed": bool(goal.last_reviewed_at),
                }
            )
        if not state.goals:
            gaps.append("No goals are set for this department.")
        elif not any(g.current for g in state.goals):
            gaps.append("None of my goals has a recorded current value, so I cannot tell progress from intent.")

    feed_types = FEEDS_BY_DEPARTMENT.get(profile.department_slug, ())
    for feed_type in feed_types:
        feed = feed_type()
        connected = feed.configured()
        snapshot["live_feeds"].append({"feed": feed.name, "connected": connected})
        if not connected:
            gaps.append(f"{feed.name} is not connected ({feed.required_config()} unset); I cannot see those figures.")
    if not feed_types:
        gaps.append("No live data feeds are mapped to my department; I only know what I am told or shown.")

    try:
        from openexecutive.onboarding.profile_builder import load_or_create_profile

        profile_set = not load_or_create_profile().is_empty()
    except Exception:  # noqa: BLE001
        profile_set = False
    snapshot["company_profile_configured"] = profile_set
    if not profile_set:
        gaps.append("The company profile is not filled in, so I am missing basic company context.")

    gaps.append("I have not been given any of your own documents yet; I do not know what you have written or decided.")
    return snapshot


def build_interview_guide(seat: str, *, review: bool) -> dict[str, Any]:
    """The agenda for the introduction (or the periodic review of it)."""
    profile = SEATS[seat]
    phases = [
        {
            "phase": "1. Introduce yourself plainly",
            "do": (
                f"Say you are their {profile.title}: there to support them, not replace or second-guess "
                "their ownership. One short paragraph. Say what you will and will not do (you propose, "
                "they decide; nothing leaves the building without them)."
            ),
        },
        {
            "phase": "2. Show what you know and what you cannot see",
            "do": (
                "Read back the knowledge snapshot in plain language: your charter and goals, the data you "
                "can and cannot see, and the gaps. Ask them to correct anything wrong. Do not pretend to "
                "know more than the snapshot says."
            ),
        },
        {
            "phase": "3. Learn what you still need",
            "ask_from": list(profile.asks),
            "do": "Ask what you want to know and why you want it. Offer to take documents, links or a walkthrough.",
        },
        {
            "phase": "4. Learn how they want to work with you",
            "ask_from": list(_HOW_TO_WORK),
        },
        {
            "phase": "5. Agree on the first month",
            "do": (
                "Agree on the first two or three things you will do for them, how you will show your work, "
                "and what 'useful' looks like a month from now."
            ),
        },
        {
            "phase": "6. Read it back and get an explicit yes",
            "do": (
                "Summarize the working agreement in under 300 words: how to work together, what you may do "
                "unasked, what stays private, first tasks, and open questions. Ask 'is that right?'. Only "
                "after they confirm, call save_working_agreement. If they want to stop, save it as deferred."
            ),
        },
    ]
    return {
        "style": (
            "Conversational and Socratic. Ask two or three questions at a time, never the whole list. "
            "Listen first, summarize back, and let their answers decide what you ask next. Skip anything "
            "they have already answered. It is fine to run this over several sessions."
        ),
        "mode": "review" if review else "first_introduction",
        "review_note": (
            "An agreement already exists. Ask what is working, what is not, and what has changed; update "
            "only what they want changed."
            if review
            else None
        ),
        "phases": phases,
    }


def pending_introduction_hint(person_id: int | None, db_path: Path | None = None) -> str | None:
    """A one-paragraph nudge for the speaker block when the speaker owns an un-introduced seat."""
    if person_id is None:
        return None
    lines: list[str] = []
    try:
        for seat in PARTNER_SEATS:
            owner = owner_for_seat(seat, db_path=db_path)
            if owner is None or owner.id != person_id:
                continue
            agreement: Agreement | None = get_agreement(seat, db_path=db_path)
            status = agreement.status if agreement else "not_started"
            profile = SEATS[seat]
            if status in ("not_started", "in_progress"):
                lines.append(
                    f"Their {profile.title} seat has not been introduced yet "
                    f"(seat='{seat}', {'interview under way' if status == 'in_progress' else 'not started'}). "
                    "Offer once, early, to spend a few minutes setting up how you will work with them "
                    "(skip this if you have already offered in this conversation); if they agree, call begin_partner_introduction. If they say "
                    "not now, call save_working_agreement with status='deferred' and do not raise it again "
                    "unless they ask."
                )
            elif agreement is not None and agreement.review_due():
                lines.append(
                    f"Their {profile.title} working agreement is due for review (seat='{seat}'). "
                    "Offer a short check-in on whether it still fits; if yes, call begin_partner_introduction."
                )
    except Exception:  # noqa: BLE001 - a hint must never break a chat turn.
        logger.warning("partners: pending-introduction hint failed", exc_info=True)
        return None
    return "\n".join(lines) if lines else None
