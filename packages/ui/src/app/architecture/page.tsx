'use client';

import { useEffect, useRef, useState } from 'react';

import DynamicSection from '@/components/architecture/DynamicSection';

// The section nav is hardcoded so the sidebar renders instantly without
// waiting for the backend. IDs must match the SECTIONS registry in
// packages/core/openexecutive/architecture/sections.py.
const SECTIONS = [
  // "Without the Jargon" — plain-language landing cluster for non-engineers.
  { id: 'nojargon_what_it_is', label: 'Without the Jargon: What It Is', sub: 'The 90-second version. A virtual executive with a council of specialists.' },
  { id: 'nojargon_authority', label: 'Without the Jargon: How It Decides To Act', sub: 'Three modes — act on its own, propose for approval, or escalate to you.' },
  { id: 'nojargon_proactive', label: "Without the Jargon: When You're Not Watching", sub: "Morning brief, check-ins, nudges — Open Executive doesn't wait to be asked." },
  { id: 'nojargon_org', label: 'Without the Jargon: Who Approves What', sub: 'Departments, heads, and approval tags — how proposals find the right person.' },
  { id: 'overview', label: 'System Overview', sub: 'High-level topology: clients, API, orchestrator, specialist agents, knowledge layer.' },
  { id: 'lifecycle', label: 'Request Lifecycle', sub: 'Full round-trip of a single chat message, including tool-use loop and parallel specialist calls.' },
  { id: 'agents', label: 'Agent Council', sub: 'The Executive orchestrator and its specialist sub-agents — roles, models, and domains.' },
  { id: 'caching', label: 'Prompt Caching', sub: 'How the system prompt is partitioned for Anthropic prompt caching, and what breaks the cache.' },
  { id: 'rag', label: 'Knowledge & RAG', sub: 'ChromaDB collections, per-specialist domain filtering, and where retrieved context is injected.' },
  { id: 'review', label: 'SME Knowledge Review', sub: 'The pending-review queue, priority ordering, and how rejected/approved items affect retrieval.' },
  { id: 'memory', label: 'Memory System', sub: 'Episodic SQLite memory (decisions, initiatives, advice, scheduled actions) and how it’s surfaced.' },
  { id: 'peer_memory', label: 'Peer Memory (Person + Department)', sub: 'External peer-keyed memory. Per-person scope keyed by Person.id for cross-channel continuity, and per-department scope keyed by department_<slug> for institutional voice. Dialectic prefetch, fire-and-forget sync, peer-graph cross-pollination, per-fixture workspace isolation.' },
  { id: 'org', label: 'Org Structure', sub: 'Departments, goals, checklists, cadences; people registry; authority gates and channel resolution (Discord/Telegram/email).' },
  { id: 'audit', label: 'Audit Log', sub: 'Searchable, append-only record of chat turns, specialist consults, tool calls, scheduled actions, alerts, and inbound integrations.' },
  { id: 'schemas', label: 'Data Schemas', sub: 'Key Pydantic models and database tables — the shape of the data flowing through the system.' },
  { id: 'workflows', label: 'Workflows', sub: 'Deterministic, multi-step orchestrations that produce structured artifacts, including the wait-for-human pause primitive.' },
  { id: 'scheduler', label: 'Scheduler & Cadences', sub: 'The async scheduler runner, cadence DSL, scheduled_actions, and proactive nudges through the user’s last-used channel.' },
  { id: 'integrations', label: 'Integrations', sub: 'External channels (Slack, Discord, email, Telegram, Google Chat, MCP gateway), attachments, and how they connect.' },
  { id: 'external_monitoring', label: 'External Monitoring', sub: 'Polls the watchlist (vendor status pages, RSS / Atom feeds, stock tickers) and routes qualifying signals through the same alert pipeline as inbound email / Slack. Watchlist editable from chat.' },
  { id: 'today', label: 'Today / Morning Brief', sub: 'The /today route — per-department goal health, a roster with awaiting-action counts, and proposals routed to a person.' },
  { id: 'api', label: 'API Reference', sub: 'The FastAPI HTTP surface — endpoints grouped by router.' },
  { id: 'mcp_server', label: 'MCP Server', sub: 'Open Executive exposed as an MCP server — company context as resources and the specialist council as tools, over Streamable-HTTP at /mcp for external agents.' },
  { id: 'user_guide', label: 'User Guide Surface', sub: "The /guide page — plain-language, per-feature overviews served from static prebuilt JSON, sharing this page's loader and renderer but separate from this technical reference." },
  { id: 'talent', label: 'Talent / Executive Search', sub: 'The in-house hiring vertical: engagements (searches), candidates, the ChromaDB matching graph, and the draft-and-approve recruiting workflows — surfaced in the /talent UI.' },
  { id: 'staff_onboarding', label: 'Staff Onboarding', sub: 'Role-tailored onboarding for new hires: reusable templates, per-hire plans with phased task checklists, the role_onboarding brief workflow, a bounded ramp drip, and chat + /today integration.' },
  { id: 'clients', label: 'Client Companies (Slots)', sub: 'Multi-client mode for fractional executives: named save files of the full company context, one active at a time, with save-back switching and per-client MCP tool configs.' },
  { id: 'security', label: 'Execution Security (Grimdall)', sub: 'Optional deterministic pre-execution guardrails on tool calls — secret denial, egress allowlist, destructive block, per-session spend budget. Shadow mode logs signed receipts; enforce mode blocks.' },
];

interface SectionMeta {
  id: string;
  fresh: boolean;
  generated_at: string | null;
}

function DiagramLegend() {
  const items: { label: string; color: string; border: string }[] = [
    { label: 'Entry / Client', color: '#1e3a8a', border: '#60a5fa' },
    { label: 'Compute / Agent', color: '#312e81', border: '#a5b4fc' },
    { label: 'Storage', color: '#365314', border: '#a3e635' },
    { label: 'Cached', color: '#713f12', border: '#facc15' },
    { label: 'External', color: '#3f3f46', border: '#a1a1aa' },
    { label: 'Hot / Not cached', color: '#7f1d1d', border: '#fca5a5' },
  ];
  return (
    <div className="rounded-lg bg-surface border border-line px-4 py-3">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
        Diagram legend
      </p>
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {items.map((it) => (
          <div key={it.label} className="flex items-center gap-1.5 text-xs">
            <span
              className="inline-block w-3 h-3 rounded-sm"
              style={{ background: it.color, border: `1.5px solid ${it.border}` }}
            />
            <span className="text-fg-muted">{it.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function ArchitecturePage() {
  const [activeSection, setActiveSection] = useState('overview');
  const [sectionMeta, setSectionMeta] = useState<Record<string, SectionMeta>>({});
  const observerRef = useRef<IntersectionObserver | null>(null);

  // Single cheap listing call — no generation triggered.
  useEffect(() => {
    fetch('/api/backend/architecture/sections')
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((data: { sections: SectionMeta[] }) => {
        const map: Record<string, SectionMeta> = {};
        for (const s of data.sections) map[s.id] = s;
        setSectionMeta(map);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    observerRef.current?.disconnect();
    observerRef.current = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setActiveSection(e.target.id);
      },
      { rootMargin: '-20% 0px -70% 0px', threshold: 0 }
    );
    SECTIONS.forEach(({ id }) => {
      const el = document.getElementById(id);
      if (el) observerRef.current?.observe(el);
    });
    return () => observerRef.current?.disconnect();
  }, []);

  const freshCount = Object.values(sectionMeta).filter((s) => s.fresh).length;
  const totalCount = SECTIONS.length;

  return (
    <div className="flex flex-1 min-h-0 bg-surface text-fg overflow-hidden">
      <aside className="w-52 flex-shrink-0 border-r border-line flex flex-col bg-surface-elevated">
        <div className="px-3 py-4">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
            Architecture
          </p>
          <nav className="space-y-0.5">
            {SECTIONS.map(({ id, label }) => {
              const meta = sectionMeta[id];
              // Content ships with the image, so a section is either
              // present (green) or, if a file is somehow missing, absent (grey).
              const dotColor = meta?.fresh ? 'bg-emerald-500/60' : 'bg-surface-input';
              return (
                <a
                  key={id}
                  href={`#${id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                  }}
                  className={`flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs transition-colors ${
                    activeSection === id
                      ? 'bg-indigo-500/10 text-indigo-400'
                      : 'text-fg-muted hover:text-fg hover:bg-surface-overlay/60'
                  }`}
                >
                  <span className={`inline-block w-1.5 h-1.5 rounded-full ${dotColor}`} />
                  <span>{label}</span>
                </a>
              );
            })}
          </nav>
        </div>

        <div className="mt-auto px-4 py-4 border-t border-line space-y-1.5">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
            Reference
          </p>
          <div className="flex justify-between text-xs">
            <span className="text-fg-subtle">Sections</span>
            <span className="text-fg-muted font-mono">{freshCount} / {totalCount}</span>
          </div>
          <p className="text-[10px] text-fg-subtle leading-relaxed">
            A map of how the running system is built — components, data flow, and the invariants that hold it together.
          </p>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-8 py-10 space-y-20">
          <div className="space-y-4">
            <div>
              <h1 className="text-2xl font-bold text-fg">Open Executive — Architecture</h1>
              <p className="mt-2 text-sm text-fg-muted">
                A reference map of how Open Executive is built — the components, data flow, and invariants of the running system, from the Executive orchestrator and its specialist council to the knowledge, memory, scheduling, and integration layers.
              </p>
            </div>
            <DiagramLegend />
          </div>

          {SECTIONS.map(({ id, label, sub }) => (
            <DynamicSection key={id} id={id} title={label} sub={sub} />
          ))}
        </div>
      </main>
    </div>
  );
}
