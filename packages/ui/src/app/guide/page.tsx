'use client';

import { useEffect, useRef, useState } from 'react';

import DynamicSection from '@/components/architecture/DynamicSection';

// The section nav is hardcoded so the sidebar renders instantly without
// waiting for the backend. IDs must match the GUIDE_SECTIONS registry in
// packages/core/openexecutive/guide/sections.py.
const SECTIONS = [
  { id: 'chat', label: 'Chat & Briefing', sub: "The main surface — talk to the Executive, and land on a briefing of what's happened." },
  { id: 'ask_oe', label: 'Ask OE', sub: 'The page-aware assistant panel — explains any screen and fills forms for you to review.' },
  { id: 'today', label: 'Today / Morning Brief', sub: 'What needs you right now: proposals, department health, and people with open items.' },
  { id: 'pulse', label: 'Pulse (Memory)', sub: "The Executive's running memory — decisions made, initiatives in flight, advice gathered." },
  { id: 'review', label: 'Review Queue', sub: 'Built-in knowledge is trusted by default. Review what you upload or edit, or send a domain for review yourself.' },
  { id: 'jobs', label: 'Jobs (Workflows)', sub: 'Multi-step workflows that produce a deliverable — board prep, GTM plan, perf review.' },
  { id: 'artifacts', label: 'Artifacts', sub: 'Your library of finished documents — drafts and workflow outputs in one place.' },
  { id: 'watchlist', label: 'Watch List', sub: 'External monitors — stock tickers, RSS feeds, status pages, web queries — that raise alerts.' },
  { id: 'departments', label: 'Departments', sub: 'Org units, each with goals, an authority level, and a specialist behind it.' },
  { id: 'people', label: 'People', sub: 'Your roster — who the Executive coordinates with, their SLAs, channels, and approval scopes.' },
  { id: 'company_profile', label: 'Company Profile & Onboarding', sub: "Your company's identity and strategy — set up once, edited any time." },
  { id: 'knowledge', label: 'Knowledge base', sub: 'Upload company documents so the Executive can ground its answers in your context.' },
  { id: 'skills', label: 'Skills', sub: 'Reusable how-to procedures the Executive can pull up — checklists, playbooks, templates.' },
  { id: 'council', label: 'Agent Council', sub: "Configure the specialists — models, prompts, reasoning depth, and the Executive's voice." },
  { id: 'audit', label: 'Audit Log', sub: 'A searchable record of every turn, consult, tool call, alert, and scheduled action.' },
  { id: 'token_usage', label: 'Token Usage', sub: 'Where your spend goes — tokens and cost by day, model, and session.' },
  { id: 'simulator', label: 'Company Simulator', sub: 'Load a realistic test company to try the Executive before trusting it with real data.' },
  { id: 'clients', label: 'Client Companies', sub: 'Multi-client mode for fractional work — switch the live company between named client slots.' },
  { id: 'integrations', label: 'Integrations', sub: 'Reach the Executive where you already work — Slack, Discord, Telegram, email, Google Chat, MCP.' },
  { id: 'settings', label: 'Settings & Advanced', sub: 'The hub for power-user tools that sit outside the day-to-day nav — including this guide.' },
];

interface SectionMeta {
  id: string;
  fresh: boolean;
  generated_at: string | null;
}

export default function GuidePage() {
  const [activeSection, setActiveSection] = useState('chat');
  const [sectionMeta, setSectionMeta] = useState<Record<string, SectionMeta>>({});
  const observerRef = useRef<IntersectionObserver | null>(null);

  // Single cheap listing call — no generation triggered.
  useEffect(() => {
    fetch('/api/backend/guide/sections')
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
            User Guide
          </p>
          <nav className="space-y-0.5">
            {SECTIONS.map(({ id, label }) => {
              const meta = sectionMeta[id];
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
            <span className="text-fg-subtle">Features</span>
            <span className="text-fg-muted font-mono">{freshCount} / {totalCount}</span>
          </div>
          <p className="text-[10px] text-fg-subtle leading-relaxed">
            Plain-language overviews of what each feature is and what it does. For how the system is
            built, see the Architecture reference.
          </p>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-8 py-10 space-y-20">
          <div>
            <h1 className="text-2xl font-bold text-fg">Open Executive — User Guide</h1>
            <p className="mt-2 text-sm text-fg-muted">
              A quick tour of every feature: what it is, and what it does for you. Not a manual —
              just enough to know where to go and why. For the technical internals, see the
              Architecture reference.
            </p>
          </div>

          {SECTIONS.map(({ id, label, sub }) => (
            <DynamicSection key={id} id={id} title={label} sub={sub} basePath="guide" />
          ))}
        </div>
      </main>
    </div>
  );
}
