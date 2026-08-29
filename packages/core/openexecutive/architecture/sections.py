"""The fixed registry of architecture-page sections.

The section IDs must match the SECTIONS const in
packages/ui/src/app/architecture/page.tsx so deep links keep working
and the sidebar renders instantly without waiting for the backend.
Each entry tells the generator three things:

* `kb_query`   – seed query passed to the RAG retriever to fetch
  relevant context chunks for grounding.
* `wants_mermaid` – whether this section should include a diagram.
* `diagram_kind` – `"flowchart"` or `"sequence"`; passed to the prompt
  so Claude picks the right Mermaid dialect.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

DiagramKind = Literal["flowchart", "sequence"]


class SectionSpec(BaseModel):
    id: str
    title: str
    sub: str
    kb_query: str
    wants_mermaid: bool
    diagram_kind: DiagramKind | None = None


SECTIONS: list[SectionSpec] = [
    # ── "Without the Jargon" cluster ──────────────────────────────────
    # Plain-language landing sections for non-engineers being walked
    # through the system. Tone + content rails live in
    # architecture-facts.yaml → `for_executives:`. Each one ends with
    # a "see also" pointer to the deeper technical sibling below.
    SectionSpec(
        id="nojargon_what_it_is",
        title="Without the Jargon: What It Is",
        sub="The 90-second version. A virtual executive with a council of specialists.",
        kb_query="virtual executive specialist council elevator overview",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="nojargon_authority",
        title="Without the Jargon: How It Decides To Act",
        sub="Three modes — act on its own, propose for approval, or escalate to you.",
        kb_query="act propose escalate authority gate three modes",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="nojargon_proactive",
        title="Without the Jargon: When You're Not Watching",
        sub="Morning brief, check-ins, nudges — Open Executive doesn't wait to be asked.",
        kb_query="proactive scheduler morning brief nudges cadences",
        wants_mermaid=True,
        diagram_kind="sequence",
    ),
    SectionSpec(
        id="nojargon_org",
        title="Without the Jargon: Who Approves What",
        sub="Departments, heads, and approval tags — how proposals find the right person.",
        kb_query="departments heads authority scope find approvers",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    # ── Technical sections (existing) ─────────────────────────────────
    SectionSpec(
        id="overview",
        title="System Overview",
        sub="High-level topology: clients, API, orchestrator, specialist agents, knowledge layer.",
        kb_query="multi-agent system architecture overview",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="lifecycle",
        title="Request Lifecycle",
        sub="Full round-trip of a single chat message, including tool-use loop and parallel specialist calls.",
        kb_query="chat request lifecycle tool use loop",
        wants_mermaid=True,
        diagram_kind="sequence",
    ),
    SectionSpec(
        id="agents",
        title="Agent Council",
        sub="The Executive orchestrator and its specialist sub-agents — roles, models, and domains.",
        kb_query="specialist agents roles domains",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="caching",
        title="Prompt Caching",
        sub="How the system prompt is partitioned for Anthropic prompt caching, and what breaks the cache.",
        kb_query="anthropic prompt caching cache_control system prompt blocks",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="rag",
        title="Knowledge & RAG",
        sub="ChromaDB collections, per-specialist domain filtering, and where retrieved context is injected.",
        kb_query="ChromaDB knowledge retrieval RAG domain filter",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="review",
        title="SME Knowledge Review",
        sub="The pending-review queue, priority ordering, and how rejected/approved items affect retrieval.",
        kb_query="knowledge review queue SME approval rejection priority",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="memory",
        title="Memory System",
        sub="Episodic SQLite memory (decisions, initiatives, advice, scheduled actions) and how it's surfaced.",
        kb_query="episodic memory decisions initiatives sqlite",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="peer_memory",
        title="Peer Memory (Person + Department)",
        sub="External peer-keyed memory layer. Two scopes: per-person (cross-channel continuity keyed by Person.id) and per-department (institutional voice keyed by department_<slug>). Dialectic prefetch, fire-and-forget sync, peer-graph cross-pollination, per-fixture workspace isolation, audit trail.",
        kb_query="peer memory person department peer card prefetch sync_turn sync_department_turn append_department_note cross channel workspace",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="org",
        title="Org Structure (Departments & People)",
        sub="Departments, goals, checklists, cadences; people registry; authority gates and channel resolution (Discord/Telegram/email).",
        kb_query="departments people authority gate cadence head persona scope tokens",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="audit",
        title="Audit Log",
        sub="Searchable, append-only record of chat turns, specialist consults, tool calls, scheduled actions, alerts, and inbound integrations.",
        kb_query="audit log events sqlite searchable trail",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="schemas",
        title="Data Schemas",
        sub="Key Pydantic models and database tables — the shape of the data flowing through the system.",
        kb_query="pydantic models database schema",
        wants_mermaid=False,
    ),
    SectionSpec(
        id="workflows",
        title="Workflows",
        sub="Deterministic, multi-step orchestrations that produce structured artifacts (board prep, quarterly plan, perf review), including the wait-for-human pause primitive.",
        kb_query="workflows board prep quarterly plan performance review wait_for_human resumer",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="scheduler",
        title="Scheduler & Cadences",
        sub="The async scheduler runner, cadence DSL (weekly@DOW-HH:MM, quarterly@DD-HH:MM), scheduled_actions, and proactive nudge delivery through the user's last-used channel.",
        kb_query="scheduler cadence scheduled actions proactive nudge runner",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="integrations",
        title="Integrations",
        sub="External channels (Slack, Discord, email, Telegram, Google Chat, MCP gateway), cross-platform attachments, and how they connect to the Executive.",
        kb_query="integrations slack discord email telegram google chat MCP gateway attachments",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="external_monitoring",
        title="External Monitoring",
        sub=(
            "How OE watches the outside world. A periodic scan polls the "
            "watchlist's vendor status pages, RSS / Atom feeds, and stock "
            "tickers; qualifying signals flow into the existing alert "
            "pipeline so they surface as briefing proposals on the same "
            "path as inbound email / Slack. The watchlist is editable "
            "from chat, and a `watchlist_research` workflow uses the "
            "7-specialist council with web_search to propose what to "
            "watch for THIS company."
        ),
        kb_query=(
            "external monitoring watchlist signal vendor status rss stock "
            "external_monitor_scan promote_signal_to_alert SSRF "
            "external_signal_received external_signal_promoted "
            "watchlist_research propose_watchlist_entries specialist "
            "research onboarding"
        ),
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="today",
        title="Today / Morning Brief",
        sub="The /today route and API: per-department goal health, a roster with awaiting-action counts, and proposals routed to a person.",
        kb_query="today morning brief department health roster proposals awaiting",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="api",
        title="API Reference",
        sub="The FastAPI HTTP surface — endpoints grouped by router, with brief descriptions of each.",
        kb_query="fastapi http endpoints routers",
        wants_mermaid=False,
    ),
    SectionSpec(
        id="mcp_server",
        title="MCP Server",
        sub="Open Executive exposed as an MCP server: company-grounded context as resources and the specialist council as tools, over Streamable-HTTP at /mcp for external agents (Claude Desktop, Cursor, Claude Code).",
        kb_query="MCP server streamable http resources tools consult_specialist mount fastmcp x-api-key",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    SectionSpec(
        id="user_guide",
        title="User Guide Surface",
        sub="The /guide page — plain-language, per-feature overviews served from static prebuilt JSON, sharing the architecture page's loader and renderer but separate from this technical reference.",
        kb_query="user guide feature overview prebuilt static sections plain language settings tile",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    # `talent` — the talent-intelligence / executive-search vertical built on
    # OE (new top-level module under packages/core; see `talent:` in
    # architecture-facts.yaml). Phases 1-3: entities + store, candidate
    # matching graph, and recruiting-automation workflows, plus the /talent UI.
    SectionSpec(
        id="talent",
        title="Talent / Executive Search",
        sub="The executive-search vertical: clients, engagements, candidates, the ChromaDB matching graph, and the draft-and-approve recruiting workflows — surfaced in the /talent UI.",
        kb_query="talent executive search candidate engagement client pipeline matching outreach screening",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    # `staff_onboarding` — onboarding a person INTO the company (distinct from
    # the company-setup wizard in `onboarding/` and the people roster). New
    # top-level module; see `staff_onboarding:` in architecture-facts.yaml.
    SectionSpec(
        id="staff_onboarding",
        title="Staff Onboarding",
        sub="Role-tailored onboarding for new hires: reusable templates, per-hire plans with phased task checklists, the role_onboarding brief workflow, a bounded ramp drip, and chat + /today integration.",
        kb_query="staff onboarding new hire ramp plan template task checklist welcome brief role_onboarding completion",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    # `clients` — named client-company slots for fractional / multi-client
    # use. New top-level module; see `clients:` in architecture-facts.yaml.
    SectionSpec(
        id="clients",
        title="Client Companies (Slots)",
        sub="Multi-client mode for fractional executives: named save files of the full company context, one active at a time, with save-back switching and per-client MCP tool configs.",
        kb_query="client slots fractional multi-client switch save restore activate company context",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
    # `security` — optional Grimdall execution guardrails on the tool-use
    # boundary. New top-level module; see `security:` in architecture-facts.yaml.
    SectionSpec(
        id="security",
        title="Execution Security (Grimdall)",
        sub="Optional, deterministic pre-execution guardrails on tool calls: secret denial, egress allowlist, destructive block, and a per-session spend budget. Shadow mode logs signed receipts; enforce mode blocks with an error tool_result.",
        kb_query="grimdall execution security guardrails secret denial egress allowlist destructive spend shadow enforce",
        wants_mermaid=True,
        diagram_kind="flowchart",
    ),
]


_SECTION_INDEX = {s.id: s for s in SECTIONS}


def get_section(section_id: str) -> SectionSpec:
    spec = _SECTION_INDEX.get(section_id)
    if spec is None:
        raise KeyError(f"Unknown architecture section: {section_id}")
    return spec
