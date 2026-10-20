"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import { getValueSummary, type ValueSummary } from "@/lib/api";
import { headline, periodNames, visibleLines } from "@/lib/valueSummary";
import { Skeleton } from "./shared";

// What the Executive did for the owner this month (GET /today/value): the
// count line at the top of Pulse's Done for you panel and the full count on
// its own screen (`/memories?tab=value`). Every number is counted from the
// record; the endpoint is the owner's alone, so for anyone else (a 403) the
// line doesn't render.

type Period = "month" | "week";

function useValueSummary(period: Period, offset: number) {
  // Each answer is kept with the request it answers, so a stale one reads as
  // still loading after the period changes (no reset inside the effect).
  const key = `${period}:${offset}`;
  const [result, setResult] = useState<{ key: string; summary: ValueSummary | null; error: boolean }>();

  useEffect(() => {
    let cancelled = false;
    getValueSummary(period, offset)
      .then((summary) => {
        if (!cancelled) setResult({ key, summary, error: false });
      })
      .catch(() => {
        if (!cancelled) setResult({ key, summary: null, error: true });
      });
    return () => {
      cancelled = true;
    };
  }, [key, period, offset]);

  // undefined while loading; null when this viewer isn't the owner (or it failed).
  const current = result?.key === key ? result : undefined;
  return { summary: current?.summary, error: current?.error ?? false };
}

/** The top line of Done for you: this month's count, opening the rest. */
export function MonthCountLine() {
  const { summary } = useValueSummary("month", 0);
  if (summary === null) return null;
  const line = summary ? headline(summary.lines) : "";
  return (
    <Link
      href="/memories?tab=value"
      className="flex items-center justify-between gap-3 rounded-xl bg-surface-overlay px-3 py-2.5 my-2 text-[15px] font-semibold text-fg hover:bg-surface transition-colors"
    >
      {summary === undefined ? (
        <Skeleton className="h-5 w-64" />
      ) : (
        <span className="min-w-0">
          {line ? `${summary.label}: ${line}` : `Nothing counted yet for ${summary.label}`}
        </span>
      )}
      <Icon name="chevron-right" size="w-4 h-4" className="shrink-0 text-accent" />
    </Link>
  );
}

/** The full screen: every counted line, against the period before. */
export function ValueScreen() {
  const [period, setPeriod] = useState<Period>("month");
  const [offset, setOffset] = useState(0);
  const { summary, error } = useValueSummary(period, offset);

  const names = (s: ValueSummary) => periodNames(s.period, s.label);

  function pick(next: Period) {
    setPeriod(next);
    setOffset(0);
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex rounded-xl border border-line p-1" role="group" aria-label="Period">
          {(["month", "week"] as const).map((p) => (
            <button
              key={p}
              type="button"
              aria-pressed={period === p}
              onClick={() => pick(p)}
              className={`h-9 rounded-lg px-3 text-sm font-semibold transition-colors ${
                period === p ? "bg-accent text-white" : "text-fg-muted hover:bg-surface-overlay"
              }`}
            >
              {p === "month" ? "Month" : "Week"}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setOffset((o) => o + 1)}
            className="h-10 w-10 inline-flex items-center justify-center rounded-xl border border-line hover:bg-surface-overlay"
            aria-label={`Previous ${period}`}
          >
            <Icon name="chevron-left" size="w-4 h-4" />
          </button>
          <span className="min-w-[9rem] text-center text-[15px] font-semibold text-fg">
            {summary ? summary.label : " "}
          </span>
          <button
            type="button"
            onClick={() => setOffset((o) => Math.max(0, o - 1))}
            disabled={offset === 0}
            className="h-10 w-10 inline-flex items-center justify-center rounded-xl border border-line hover:bg-surface-overlay disabled:opacity-40"
            aria-label={`Next ${period}`}
          >
            <Icon name="chevron-right" size="w-4 h-4" />
          </button>
        </div>
      </div>

      {summary === undefined ? (
        <Skeleton className="h-48 w-full" />
      ) : summary === null ? (
        <p className="text-[15px] text-fg-muted">
          {error ? "Couldn't load the summary. Try again in a moment." : "Only the owner sees this summary."}
        </p>
      ) : (
        <>
          <section className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-6">
            {visibleLines(summary.lines).length === 0 ? (
              <p className="text-[15px] text-fg-muted">Nothing counted for {summary.label}.</p>
            ) : (
              <>
                <h2 className="text-sm font-medium text-fg-subtle mb-1 sm:hidden">
                  What it did in {names(summary).heading}
                </h2>
                <table className="w-full text-[15px]">
                  <thead className="max-sm:sr-only">
                    <tr className="text-left text-sm text-fg-subtle">
                      <th className="font-medium pb-2">What it did</th>
                      <th className="font-medium pb-2 pl-3 text-right">{names(summary).current}</th>
                      <th className="font-medium pb-2 pl-3 text-right">{names(summary).previous}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleLines(summary.lines).map((l) => (
                      <tr key={l.key} className="border-t border-line max-sm:first:border-t-0">
                        <td className="py-3 text-fg">{l.label}</td>
                        <td className="py-3 pl-3 text-right align-middle">
                          <span className="block text-2xl leading-tight font-bold tabular-nums text-fg">{l.count}</span>
                          <span className="block text-sm tabular-nums text-fg-muted sm:hidden">
                            {names(summary).previousShort} {l.previous}
                          </span>
                        </td>
                        <td className="py-3 pl-3 text-right tabular-nums text-fg-muted max-sm:hidden">
                          {l.previous}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </section>
          {summary.deliverables.length > 0 && (
            <section className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-6">
              <h2 className="text-lg font-semibold text-fg mb-2">
                Reports and documents
                {summary.deliverables_total > summary.deliverables.length &&
                  ` (latest ${summary.deliverables.length} of ${summary.deliverables_total})`}
              </h2>
              <ul>
                {summary.deliverables.map((d) => (
                  <li key={d.run_id} className="border-t border-line first:border-t-0">
                    <Link
                      href={`/jobs/runs/${encodeURIComponent(d.run_id)}`}
                      className="flex items-center justify-between gap-3 py-3 text-[15px] text-fg hover:text-accent"
                    >
                      <span className="min-w-0 truncate">{d.title}</span>
                      <span className="shrink-0 text-sm text-fg-subtle tabular-nums">
                        {new Date(d.finished_at).toLocaleDateString()}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <p className="text-sm text-fg-subtle">
            Counted from what was recorded: messages that went out, replies, meetings, briefs,
            reports and tasks. Nothing here is estimated.
          </p>
        </>
      )}
    </div>
  );
}
