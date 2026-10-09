"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { getActivity, getHistory, listStandingFacts, type ActivityItem } from "@/lib/api";
import Icon from "@/components/Icon";
import { upcomingLabel, whenLabel, type MemoryView } from "@/lib/pulseView";
import { SUMMARY_KINDS, activityLine } from "./CadenceSection";
import type { PulseData } from "./PulseHeader";
import { Skeleton, metaFor } from "./shared";

// The Pulse overview's three blocks under the heartbeat: the next few things
// the Executive will do (Coming up), the last few it did (Done recently), and
// a tile per kind of memory. Each links to its full list on its own screen
// (`/memories?tab=…`), so the overview stays short.

const PREVIEW_ROWS = 4;

/** A card with a heading and a "see all" link in its top-right corner. */
function PreviewCard({
  title,
  href,
  linkLabel,
  children,
}: {
  title: string;
  href: string;
  linkLabel: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-6 min-w-0">
      <div className="flex items-center justify-between gap-3 mb-2">
        <h2 className="text-lg font-semibold text-fg">{title}</h2>
        <Link
          href={href}
          className="inline-flex h-10 items-center gap-1 rounded-xl px-2 -mr-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
        >
          {linkLabel}
          <Icon name="chevron-right" size="w-4 h-4" />
        </Link>
      </div>
      {children}
    </section>
  );
}

/** One row: a short "when" on the left, what on the right. */
function PreviewRow({
  when,
  upcoming,
  title,
  detail,
  chip,
}: {
  when: string;
  upcoming: boolean;
  title: string;
  detail?: string;
  chip?: string;
}) {
  return (
    <li className="flex items-start gap-3 py-3 border-t border-line first:border-t-0">
      <span
        className={`w-20 shrink-0 text-sm tabular-nums pt-0.5 ${
          upcoming ? "font-semibold text-accent" : "font-medium text-fg-subtle"
        }`}
      >
        {when}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[15px] font-semibold text-fg break-words line-clamp-2" title={title}>
          {title}
          {chip && (
            <span className="ml-2 inline-block rounded-md border border-accent/40 px-1.5 text-xs font-medium text-accent align-[1px]">
              {chip}
            </span>
          )}
        </div>
        {detail && (
          <div className="text-sm text-fg-muted mt-0.5 line-clamp-1" title={detail}>
            {detail}
          </div>
        )}
      </div>
    </li>
  );
}

function RowsSkeleton() {
  return (
    <div className="space-y-3 py-2">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-5/6" />
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-[15px] text-fg-muted">{children}</p>;
}

/** The next few things it will do, background scans left out. */
export function ComingUp({ pulse }: { pulse: PulseData }) {
  const { data, loading } = pulse;
  const next = useMemo(
    () =>
      (data?.pending ?? [])
        .filter((a) => metaFor(a).group !== "system")
        .sort((x, y) => x.run_at.localeCompare(y.run_at))
        .slice(0, PREVIEW_ROWS),
    [data],
  );

  return (
    <PreviewCard title="Coming up" href="/memories?tab=schedule" linkLabel="Full schedule">
      {loading ? (
        <RowsSkeleton />
      ) : next.length === 0 ? (
        <Empty>Nothing scheduled yet. Briefs and check-ins appear here once they&apos;re set up.</Empty>
      ) : (
        <ul>
          {next.map((a) => {
            const meta = metaFor(a);
            const followUp = a.kind === "ad_hoc";
            return (
              <PreviewRow
                key={a.id}
                upcoming
                when={upcomingLabel(a.run_at)}
                title={
                  followUp
                    ? a.intent_text
                    : meta.group === "departments" && a.department
                      ? `${meta.label}: ${a.department}`
                      : meta.label
                }
                detail={followUp ? undefined : meta.blurb}
                chip={followUp ? "Follow-up" : undefined}
              />
            );
          })}
        </ul>
      )}
    </PreviewCard>
  );
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The last few things it did on its own. */
export function DoneRecently() {
  const [items, setItems] = useState<ActivityItem[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    getActivity(PREVIEW_ROWS)
      .then((res) => {
        if (!cancelled) setItems(res.items.slice(0, PREVIEW_ROWS));
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <PreviewCard title="Done recently" href="/memories?tab=activity" linkLabel="All activity">
      {items === null ? (
        <RowsSkeleton />
      ) : items.length === 0 ? (
        <Empty>Nothing yet. What it does on its own shows up here as it happens.</Empty>
      ) : (
        <ul>
          {items.map((it, i) => {
            const { verb, subject } = activityLine(it);
            // For summary-style kinds the subject IS the summary, so the
            // detail line says only what kind of thing it was.
            // A plain "action" has nothing to add beyond its summary.
            const detail =
              it.kind === "action"
                ? undefined
                : SUMMARY_KINDS.has(it.kind)
                  ? capitalise(verb.replace(/:$/, ""))
                  : capitalise(`${verb} ${subject}`);
            return (
              <PreviewRow
                key={`${it.at}-${it.kind}-${i}`}
                upcoming={false}
                when={whenLabel(it.at)}
                title={it.summary || detail || capitalise(verb)}
                detail={it.summary ? detail : undefined}
              />
            );
          })}
        </ul>
      )}
    </PreviewCard>
  );
}

interface TileSpec {
  view: MemoryView;
  label: string;
  count: number | null;
  detail?: string;
  flag?: string;
}

function Tile({ spec, wide }: { spec: TileSpec; wide: boolean }) {
  return (
    <Link
      href={`/memories?tab=${spec.view}`}
      className={`group rounded-2xl border border-line bg-surface p-4 sm:p-5 min-w-0 hover:border-accent/50 hover:bg-surface-overlay transition-colors ${
        wide ? "col-span-2 sm:col-span-1" : ""
      }`}
    >
      <div className="text-3xl font-bold tabular-nums text-fg leading-tight">
        {spec.count === null ? <Skeleton className="h-8 w-10" /> : spec.count}
      </div>
      <div className="mt-1 text-[15px] font-semibold text-fg">{spec.label}</div>
      {spec.flag ? (
        <span className="mt-2 inline-block rounded-full bg-amber-500/15 px-2.5 py-0.5 text-xs font-semibold text-amber-500">
          {spec.flag}
        </span>
      ) : (
        spec.detail && (
          <div className="mt-1 text-sm text-fg-muted line-clamp-2" title={spec.detail}>
            {spec.detail}
          </div>
        )
      )}
    </Link>
  );
}

/** Newest first by an ISO timestamp field. */
function newest<T>(rows: T[], at: (row: T) => string): T | null {
  return rows.reduce<T | null>((best, r) => (!best || at(r) > at(best) ? r : best), null);
}

/** A tile per kind of memory; each opens its list. */
export function MemoryTiles({ pulse }: { pulse: PulseData }) {
  const { data } = pulse;
  const [facts, setFacts] = useState<{ active: number; toApprove: number } | null>(null);
  // undefined while loading; null when this viewer has no notes (the tile goes).
  const [notes, setNotes] = useState<number | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    listStandingFacts()
      .then((page) => {
        if (cancelled) return;
        setFacts({
          active: page.facts.filter((f) => f.status === "active").length,
          toApprove: page.can_review ? page.facts.filter((f) => f.status === "proposed").length : 0,
        });
      })
      .catch(() => {
        if (!cancelled) setFacts({ active: 0, toApprove: 0 });
      });
    getHistory()
      .then((state) => {
        if (!cancelled) setNotes(state ? state.notes.length : null);
      })
      .catch(() => {
        if (!cancelled) setNotes(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const latestDecision = data ? newest(data.decisions, (d) => d.timestamp) : null;
  const latestAdvice = data ? newest(data.advice, (a) => a.timestamp) : null;
  const tiles: TileSpec[] = [
    {
      view: "decisions",
      label: "Decisions",
      count: data ? data.decisions.length : null,
      detail: latestDecision ? `Latest: ${latestDecision.summary}` : undefined,
    },
    {
      view: "initiatives",
      label: "Projects",
      count: data ? data.initiatives.length : null,
      detail: data
        ? `${data.initiatives.filter((i) => i.status === "active").length} active`
        : undefined,
    },
    {
      view: "advice",
      label: "Advice",
      count: data ? data.advice.length : null,
      detail: latestAdvice ? `Latest: ${latestAdvice.query_summary}` : undefined,
    },
    {
      view: "corrections",
      label: "Facts you taught it",
      count: facts ? facts.active : null,
      flag: facts && facts.toApprove > 0 ? `${facts.toApprove} to approve` : undefined,
      detail: "Used in every answer",
    },
  ];
  if (notes !== null) {
    tiles.push({
      view: "history",
      label: "Your notes",
      count: notes ?? null,
      detail: "Only you see these",
    });
  }

  return (
    <section aria-labelledby="pulse-memory-heading">
      <h2 id="pulse-memory-heading" className="text-lg font-semibold text-fg mb-3">
        What it remembers
      </h2>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {tiles.map((t, i) => (
          // An odd last tile spans the phone's two columns.
          <Tile key={t.view} spec={t} wide={tiles.length % 2 === 1 && i === tiles.length - 1} />
        ))}
      </div>
    </section>
  );
}

/** A focused screen behind the overview: a way back, a title, one job. */
export function FocusedScreen({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-5">
      <header>
        <Link
          href="/memories"
          className="inline-flex h-10 items-center gap-1 rounded-xl px-2 -ml-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
        >
          <Icon name="arrow-left" size="w-4 h-4" />
          Pulse
        </Link>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg mt-1">{title}</h1>
        <p className="text-[15px] text-fg-muted mt-1.5 max-w-2xl">{description}</p>
      </header>
      {children}
    </div>
  );
}
