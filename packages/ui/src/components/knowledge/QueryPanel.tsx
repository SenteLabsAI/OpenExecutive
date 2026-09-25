"use client";

import { useState } from "react";
import {
  searchKnowledge,
  type KnowledgeSearchHit,
  type KnowledgeSearchResponse,
  type KnowledgeSourceType,
} from "@/lib/api";

interface QueryPanelProps {
  domains: string[];
  onOpenFile?: (kind: "builtin" | "failures", domain: string, filename: string) => void;
}

const SPECIALISTS = [
  { id: "", label: "All specialists" },
  { id: "cso", label: "CSO (Strategy)" },
  { id: "cfo", label: "CFO (Finance)" },
  { id: "chro", label: "CHRO (HR)" },
  { id: "gc", label: "GC (Legal)" },
  { id: "coo", label: "COO (Operations)" },
  { id: "cmo", label: "CMO (Marketing)" },
  { id: "cpo", label: "CPO (Product + Strategy)" },
  { id: "sales", label: "Sales (Sales + Marketing)" },
  { id: "board_comms", label: "Board Comms (Board + Finance)" },
];

const ALL_SOURCES: KnowledgeSourceType[] = ["builtin", "company", "failures", "external"];

export default function QueryPanel({ domains, onOpenFile }: QueryPanelProps) {
  const [query, setQuery] = useState("");
  const [specialist, setSpecialist] = useState("");
  const [includes, setIncludes] = useState<Set<KnowledgeSourceType>>(new Set(ALL_SOURCES));
  const [selectedDomains, setSelectedDomains] = useState<Set<string>>(new Set());
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<KnowledgeSearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (!query.trim()) return;
    setRunning(true);
    setError(null);
    try {
      const res = await searchKnowledge({
        query: query.trim(),
        specialist: specialist || undefined,
        domain_filter: selectedDomains.size > 0 ? Array.from(selectedDomains) : undefined,
        include: Array.from(includes),
      });
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Search failed");
    } finally {
      setRunning(false);
    }
  }

  function toggleInclude(t: KnowledgeSourceType) {
    setIncludes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }

  function toggleDomain(d: string) {
    setSelectedDomains((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });
  }

  return (
    <div className="flex flex-col gap-5 max-w-4xl">
      <div>
        <h2 className="text-base font-semibold text-fg">Query mode</h2>
        <p className="text-xs text-fg-muted mt-1">
          Test what the Executive would retrieve for a given question. Distances are
          cosine — lower is closer.
        </p>
      </div>

      <div className="space-y-3 rounded-xl border border-line bg-surface-elevated/40 p-4">
        <div className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                run();
              }
            }}
            placeholder="e.g. how should we think about pricing for a new SaaS product?"
            className="flex-1 rounded-lg border border-line-strong bg-surface-elevated px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50"
          />
          <button
            onClick={run}
            disabled={!query.trim() || running}
            className="px-4 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-400 disabled:opacity-40 text-white text-sm font-medium transition-colors"
          >
            {running ? "Running…" : "Run"}
          </button>
        </div>

        <div className="flex flex-wrap gap-4">
          <div className="flex items-center gap-2">
            <label className="text-[11px] uppercase tracking-widest text-fg-muted">
              Specialist
            </label>
            <select
              value={specialist}
              onChange={(e) => setSpecialist(e.target.value)}
              className="rounded-lg border border-line-strong bg-surface-elevated px-2 py-1 text-xs text-fg focus:outline-none focus:ring-2 focus:ring-indigo-500/50"
            >
              {SPECIALISTS.map((s) => (
                <option key={s.id || "all"} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <label className="text-[11px] uppercase tracking-widest text-fg-muted">
              Include
            </label>
            {ALL_SOURCES.map((t) => (
              <button
                key={t}
                onClick={() => toggleInclude(t)}
                className={`text-xs px-2 py-1 rounded border transition-colors ${
                  includes.has(t)
                    ? "bg-indigo-500/15 text-indigo-300 border-indigo-500/40"
                    : "bg-surface-overlay/40 text-fg-muted border-line-strong"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <label className="text-[11px] uppercase tracking-widest text-fg-muted">
            Domains
          </label>
          {domains.map((d) => (
            <button
              key={d}
              onClick={() => toggleDomain(d)}
              className={`text-xs px-2 py-1 rounded border transition-colors ${
                selectedDomains.has(d)
                  ? "bg-surface-input text-fg border-line-strong"
                  : "bg-surface-overlay/40 text-fg-muted border-line-strong hover:text-fg"
              }`}
            >
              {d}
            </button>
          ))}
          {selectedDomains.size > 0 && (
            <button
              onClick={() => setSelectedDomains(new Set())}
              className="text-xs text-fg-muted hover:text-fg underline-offset-2 hover:underline"
            >
              clear
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="text-sm text-red-400 px-4 py-3 rounded-xl bg-red-950/40 border border-red-900/60">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-5">
          <div className="text-xs text-fg-muted space-y-1">
            {result.effective_domains && result.effective_domains.length > 0 ? (
              <p>
                <span className="text-fg-muted">Domain filter:</span>{" "}
                {result.effective_domains.join(", ")}
              </p>
            ) : (
              <p>
                <span className="text-fg-muted">Domain filter:</span> none (all domains)
              </p>
            )}
            <p>
              <span className="text-fg-muted">Specialists that would see these chunks:</span>{" "}
              {result.specialists_that_would_see_this.join(", ") || "—"}
            </p>
          </div>

          <ResultGroup
            title="Playbooks"
            kind="builtin"
            hits={result.builtin}
            accent="indigo"
            onOpenFile={onOpenFile}
          />
          <ResultGroup
            title="Failures"
            kind="failures"
            hits={result.failures}
            accent="rose"
            onOpenFile={onOpenFile}
          />
          <ResultGroup
            title="Company documents"
            kind="company"
            hits={result.company}
            accent="emerald"
          />
          <ResultGroup
            title="Reference Library"
            kind="external"
            hits={result.external}
            accent="amber"
          />
        </div>
      )}
    </div>
  );
}

function ResultGroup({
  title,
  kind,
  hits,
  accent,
  onOpenFile,
}: {
  title: string;
  kind: "builtin" | "failures" | "company" | "external";
  hits: KnowledgeSearchHit[];
  accent: "indigo" | "rose" | "emerald" | "amber";
  onOpenFile?: (kind: "builtin" | "failures", domain: string, filename: string) => void;
}) {
  const accentClass = {
    indigo: "text-indigo-400 border-l-indigo-500/40",
    rose: "text-rose-400 border-l-rose-500/50",
    emerald: "text-emerald-400 border-l-emerald-500/40",
    amber: "text-amber-400 border-l-amber-500/40",
  }[accent];
  const isOpenable = kind === "builtin" || kind === "failures";

  return (
    <div>
      <div className="flex items-baseline justify-between mb-2">
        <h3 className={`text-xs font-semibold uppercase tracking-widest ${accentClass.split(" ")[0]}`}>
          {title}
        </h3>
        <span className="text-[10px] text-fg-subtle">{hits.length} hit{hits.length === 1 ? "" : "s"}</span>
      </div>
      {hits.length === 0 ? (
        <p className="text-xs text-fg-subtle">No matches.</p>
      ) : (
        <div className="space-y-2">
          {hits.map((h, i) => (
            <div
              key={`${kind}-${h.filename}-${h.chunk_index ?? i}`}
              className={`rounded-lg bg-surface-elevated/60 border border-line border-l-2 px-3 py-2 ${accentClass}`}
            >
              <div className="flex items-baseline justify-between gap-3 flex-wrap">
                <div className="flex items-baseline gap-2 flex-wrap text-xs">
                  <span className="text-fg font-medium">{h.filename}</span>
                  <span className="text-fg-muted">·</span>
                  <span className="text-fg-muted">{h.domain}</span>
                  {h.publisher && (
                    <>
                      <span className="text-fg-muted">·</span>
                      <span className="text-fg-muted">{h.publisher}</span>
                    </>
                  )}
                  <span className="text-fg-muted">·</span>
                  <span className="text-fg-muted">dist {h.distance.toFixed(3)}</span>
                </div>
                {isOpenable && onOpenFile && (
                  <button
                    onClick={() => onOpenFile(kind, h.domain, h.filename)}
                    className="text-[10px] text-fg-muted hover:text-fg transition-colors"
                  >
                    open →
                  </button>
                )}
              </div>
              <p className="text-xs text-fg mt-1.5 whitespace-pre-wrap leading-relaxed">
                {h.text}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
