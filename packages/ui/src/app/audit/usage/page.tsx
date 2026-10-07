"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getAuditUsage,
  type UsageByDay,
  type UsageBySource,
  type UsageByModel,
  type UsageSummary,
  type UsageTotals,
} from "@/lib/api";

function fmtInt(n: number): string {
  return (n ?? 0).toLocaleString();
}

// Cost spans wide ranges (sub-cent per call up to dollars in aggregate), so
// show more precision when small to avoid a misleading "$0.00".
function fmtCost(n: number): string {
  const v = n ?? 0;
  if (v === 0) return "$0";
  return `$${v.toFixed(v < 1 ? 4 : 2)}`;
}

// Cache-hit ratio: prompt input served from cache as a fraction of ALL prompt
// input (fresh + cache reads + cache writes). cache_creation tokens are billed
// prompt input too, so they belong in the denominator. The whole system is
// designed around prompt caching, so this is the key cost signal.
function cacheHitPct(
  u: Pick<
    UsageTotals,
    "cache_read_input_tokens" | "input_tokens" | "cache_creation_input_tokens"
  >,
): number {
  const denom =
    u.cache_read_input_tokens + u.input_tokens + u.cache_creation_input_tokens;
  if (denom <= 0) return 0;
  return Math.round((u.cache_read_input_tokens / denom) * 100);
}

function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-line bg-surface-elevated/40 px-4 py-3">
      <div className="text-xs text-fg-muted">{label}</div>
      <div className="mt-1 text-lg font-semibold text-fg tabular-nums">{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-fg-subtle">{hint}</div> : null}
    </div>
  );
}

function UsageRowCells({ u }: { u: UsageTotals }) {
  return (
    <>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(u.calls)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(u.input_tokens)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(u.cache_read_input_tokens)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(u.cache_creation_input_tokens)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(u.output_tokens)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(u.web_search_requests ?? 0)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{cacheHitPct(u)}%</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtCost(u.cost_usd)}</td>
    </>
  );
}

const COL_HEADERS = ["Calls", "Input", "Cache read", "Cache write", "Output", "Searches", "Cached", "Cost"];

// Debounce window for refetching as the date-range filter changes (matches
// the /audit list page).
const DEBOUNCE_MS = 250;

export default function TokenUsagePage() {
  const [data, setData] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [since, setSince] = useState<string>("");
  const [until, setUntil] = useState<string>("");

  const debounceRef = useRef<number | null>(null);
  const params = useMemo(
    () => ({
      // `datetime-local` returns naive strings; the backend `ts` column is ISO
      // with offset and filters by string comparison, so normalize to ISO.
      since: since ? new Date(since).toISOString() : undefined,
      until: until ? new Date(until).toISOString() : undefined,
    }),
    [since, until],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getAuditUsage(params));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [params]);

  useEffect(() => {
    if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => void refresh(), DEBOUNCE_MS);
    return () => {
      if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    };
  }, [refresh]);

  const totals = data?.totals;
  const maxDayInput = useMemo(
    () => Math.max(1, ...(data?.by_day ?? []).map((d) => d.input_tokens + d.cache_read_input_tokens)),
    [data],
  );

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
          <div className="flex items-center justify-between gap-4">
            <h1 className="text-xl font-semibold text-fg">Token usage</h1>
            <Link
              href="/audit"
              className="text-xs text-fg-muted hover:text-fg underline-offset-2 hover:underline"
            >
              ← Audit log
            </Link>
          </div>
          <p className="mt-1 text-sm text-fg-muted">
            What the AI has cost across every conversation, added up from the
            audit log. Days are UTC. Cost is the actual OpenRouter charge captured
            per call — it accrues from when cost tracking went live, so calls
            logged before then count tokens but $0.
          </p>

          {/* Date-range filter */}
          <div className="mt-5 grid grid-cols-1 md:grid-cols-4 gap-3">
            <label className="text-xs text-fg-muted flex flex-col gap-1">
              From
              <input
                type="datetime-local"
                value={since}
                onChange={(e) => setSince(e.target.value)}
                className="px-2 py-1 rounded-lg bg-surface-elevated border border-line text-sm focus:outline-none focus:border-indigo-500"
              />
            </label>
            <label className="text-xs text-fg-muted flex flex-col gap-1">
              Until
              <input
                type="datetime-local"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
                className="px-2 py-1 rounded-lg bg-surface-elevated border border-line text-sm focus:outline-none focus:border-indigo-500"
              />
            </label>
            <div className="flex items-end">
              <button
                onClick={() => {
                  setSince("");
                  setUntil("");
                }}
                className="px-3 py-1.5 rounded-lg bg-surface-overlay hover:bg-surface-input text-sm border border-line-strong"
              >
                Clear filters
              </button>
            </div>
            <div className="text-xs text-fg-muted flex items-end pb-1.5">
              {loading ? "Loading…" : `${fmtInt(totals?.calls ?? 0)} calls`}
            </div>
          </div>

          {error ? (
            <div className="mt-6 rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
              {error}
            </div>
          ) : null}

          {/* Totals */}
          {totals ? (
            <div className="mt-6 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
              <StatCard label="Cost (USD)" value={fmtCost(totals.cost_usd)} hint="actual charged" />
              <StatCard label="Calls" value={fmtInt(totals.calls)} />
              <StatCard label="Cache hit" value={`${cacheHitPct(totals)}%`} hint="of prompt input served from cache" />
              <StatCard label="Output tokens" value={fmtInt(totals.output_tokens)} />
              <StatCard label="Fresh input" value={fmtInt(totals.input_tokens)} />
              <StatCard label="Cache read" value={fmtInt(totals.cache_read_input_tokens)} />
              <StatCard label="Cache write" value={fmtInt(totals.cache_creation_input_tokens)} />
              <StatCard label="Searches" value={fmtInt(totals.web_search_requests ?? 0)} hint="server-side web searches" />
            </div>
          ) : null}

          {/* By source */}
          {data?.scripts && data.scripts.scripts > 0 ? (
            <section className="mt-8">
              <h2 className="text-sm font-medium text-fg mb-2">Scripts</h2>
              <p className="text-xs text-fg-muted mb-3">
                When work repeats over many items, the Executive runs one short script that makes the
                tool calls itself, instead of a model turn per call.
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <StatCard
                  label="Scripts run"
                  value={fmtInt(data.scripts.scripts)}
                  hint={`${fmtInt(data.scripts.ok)} worked · ${fmtInt(data.scripts.in_workflows)} in workflows`}
                />
                <StatCard label="Tool calls in scripts" value={fmtInt(data.scripts.calls)} />
                <StatCard
                  label="Model turns avoided"
                  value={`up to ${fmtInt(data.scripts.turns_avoided)}`}
                  hint="calls that needed no model turn of their own"
                />
                <StatCard
                  label="Time in scripts"
                  value={`${(data.scripts.duration_ms / 1000).toFixed(1)}s`}
                  hint="mostly the tools' own time"
                />
              </div>
            </section>
          ) : null}

          {data && data.by_source && data.by_source.length > 0 ? (
            <section className="mt-8">
              <h2 className="text-sm font-medium text-fg mb-2">By source</h2>
              <p className="text-xs text-fg-muted mb-2">
                Which part of the system made the calls: chat turns, research specialists, the research routing and watchlist passes, triage, memory extraction.
              </p>
              <div className="rounded-xl border border-line overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-elevated/60 text-fg-muted text-xs">
                      <th className="px-3 py-2 text-left font-medium">Source</th>
                      {COL_HEADERS.map((h) => (
                        <th key={h} className="px-3 py-2 text-right font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_source.map((s: UsageBySource) => (
                      <tr key={s.source} className="border-t border-line/60">
                        <td className="px-3 py-1.5 font-mono text-xs text-fg">{s.source}</td>
                        <UsageRowCells u={s} />
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {/* By model */}
          {data && data.by_model.length > 0 ? (
            <section className="mt-8">
              <h2 className="text-sm font-medium text-fg mb-2">By model</h2>
              <div className="rounded-xl border border-line overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-elevated/60 text-fg-muted text-xs">
                      <th className="px-3 py-2 text-left font-medium">Model</th>
                      {COL_HEADERS.map((h) => (
                        <th key={h} className="px-3 py-2 text-right font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_model.map((m: UsageByModel) => (
                      <tr key={m.model} className="border-t border-line/60">
                        <td className="px-3 py-1.5 font-mono text-xs text-fg">{m.model}</td>
                        <UsageRowCells u={m} />
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {/* By day */}
          {data && data.by_day.length > 0 ? (
            <section className="mt-8">
              <h2 className="text-sm font-medium text-fg mb-2">By day (UTC)</h2>
              <div className="rounded-xl border border-line overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-surface-elevated/60 text-fg-muted text-xs">
                      <th className="px-3 py-2 text-left font-medium">Day</th>
                      {COL_HEADERS.map((h) => (
                        <th key={h} className="px-3 py-2 text-right font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_day.map((d: UsageByDay) => {
                      const pct = ((d.input_tokens + d.cache_read_input_tokens) / maxDayInput) * 100;
                      return (
                        <tr key={d.day} className="border-t border-line/60">
                          <td className="px-3 py-1.5">
                            <div className="text-xs text-fg tabular-nums">{d.day}</div>
                            <div className="mt-1 h-1 rounded bg-surface-input/60 overflow-hidden">
                              <div className="h-full bg-indigo-500/60" style={{ width: `${pct}%` }} />
                            </div>
                          </td>
                          <UsageRowCells u={d} />
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {!loading && data && data.by_model.length === 0 ? (
            <div className="mt-8 text-sm text-fg-muted">
              No token usage recorded for this range yet.
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
