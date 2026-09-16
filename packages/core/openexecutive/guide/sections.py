"""The fixed registry of user-guide sections.

The ``/guide`` page is a plain-language overview of every user-facing
feature — "what it is, what it does" — kept separate from the technical
``/architecture`` reference. Like the architecture page, each section is
rendered from a static, version-controlled JSON file under
``guide/prebuilt/<id>.json``; nothing on the serving path calls an LLM.

The section IDs must match the ``GUIDE_SECTIONS`` const in
``packages/ui/src/app/guide/page.tsx`` so deep links keep working and the
sidebar renders instantly without waiting for the backend.
"""
from __future__ import annotations

from pydantic import BaseModel


class GuideSection(BaseModel):
    id: str
    title: str
    sub: str


# Ordered roughly by the day-to-day loop: the surfaces you live in first,
# then the org/knowledge you configure, then the power-user tools.
GUIDE_SECTIONS: list[GuideSection] = [
    GuideSection(
        id="chat",
        title="Chat & Briefing",
        sub="The main surface — talk to the Executive, and land on a briefing of what's happened.",
    ),
    GuideSection(
        id="ask_oe",
        title="Ask OE",
        sub="The page-aware assistant panel — explains any screen and fills forms for you to review.",
    ),
    GuideSection(
        id="today",
        title="Today / Morning Brief",
        sub="What needs you right now: proposals, department health, and people with open items.",
    ),
    GuideSection(
        id="pulse",
        title="Pulse (Memory)",
        sub="The Executive's running memory — decisions made, initiatives in flight, advice gathered.",
    ),
    GuideSection(
        id="review",
        title="Review Queue",
        sub="Approve, reject, or correct incoming knowledge before the Executive relies on it.",
    ),
    GuideSection(
        id="jobs",
        title="Jobs (Workflows)",
        sub="Multi-step workflows that produce a deliverable — board prep, GTM plan, perf review.",
    ),
    GuideSection(
        id="artifacts",
        title="Artifacts",
        sub="Your library of finished documents — drafts and workflow outputs in one place.",
    ),
    GuideSection(
        id="watchlist",
        title="Watch List",
        sub="External monitors — stock tickers, RSS feeds, status pages, web queries — that raise alerts.",
    ),
    GuideSection(
        id="departments",
        title="Departments",
        sub="Org units, each with goals, an authority level, and a specialist behind it.",
    ),
    GuideSection(
        id="people",
        title="People",
        sub="Your roster — who the Executive coordinates with, their SLAs, channels, and approval scopes.",
    ),
    GuideSection(
        id="company_profile",
        title="Company Profile & Onboarding",
        sub="Your company's identity and strategy — set up once, edited any time.",
    ),
    GuideSection(
        id="knowledge",
        title="Knowledge Base",
        sub="Upload company documents so the Executive can ground its answers in your context.",
    ),
    GuideSection(
        id="skills",
        title="Skills",
        sub="Reusable how-to procedures the Executive can pull up — checklists, playbooks, templates.",
    ),
    GuideSection(
        id="council",
        title="Agent Council",
        sub="Configure the specialists — models, prompts, reasoning depth, and the Executive's voice.",
    ),
    GuideSection(
        id="audit",
        title="Audit Log",
        sub="A searchable record of every turn, consult, tool call, alert, and scheduled action.",
    ),
    GuideSection(
        id="token_usage",
        title="Token Usage",
        sub="Where your spend goes — tokens and cost by day, model, and session.",
    ),
    GuideSection(
        id="simulator",
        title="Company Simulator",
        sub="Load a realistic test company to try the Executive before trusting it with real data.",
    ),
    GuideSection(
        id="clients",
        title="Client Companies",
        sub="Multi-client mode for fractional work — switch the live company between named client slots.",
    ),
    GuideSection(
        id="integrations",
        title="Integrations",
        sub="Reach the Executive where you already work — Slack, Discord, Telegram, email, Google Chat, MCP.",
    ),
    GuideSection(
        id="settings",
        title="Settings & Advanced",
        sub="The hub for power-user tools that sit outside the day-to-day nav — including this guide.",
    ),
]


_SECTION_INDEX = {s.id: s for s in GUIDE_SECTIONS}


def get_section(section_id: str) -> GuideSection:
    spec = _SECTION_INDEX.get(section_id)
    if spec is None:
        raise KeyError(f"Unknown guide section: {section_id}")
    return spec
