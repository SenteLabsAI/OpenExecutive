"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { getActivity, type ActivityItem } from "@/lib/api";
import Icon from "@/components/Icon";
import { upcomingLabel, whenLabel } from "@/lib/pulseView";
import { SUMMARY_KINDS, activityLine, writeUpHref, writeUpLabel } from "./CadenceSection";
import type { PulseData } from "./PulseHeader";
import { Skeleton, metaFor } from "./shared";
import { MonthCountLine } from "./ValueSection";

// Memory → What it does, under the heartbeat: the next few things the
// Executive will do (Coming up), and the month's count and the last few it
// did (Done for you). Each links to its full list on its own screen
// (`/memories?tab=…`), so the tab stays short. Also the focused screen frame.

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
  href,
  linkLabel = "Read it",
}: {
  when: string;
  upcoming: boolean;
  title: string;
  detail?: string;
  chip?: string;
  /** A finished run's write-up: "Read it" after the detail. */
  href?: string | null;
  linkLabel?: string;
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
        {(detail || href) && (
          <div className={`text-sm text-fg-muted mt-0.5 ${href ? "line-clamp-3" : "line-clamp-1"}`} title={detail}>
            {detail}
            {href && (
              <>
                {detail && " "}
                <Link href={href} className="font-semibold text-accent hover:underline whitespace-nowrap">
                  {linkLabel}
                </Link>
              </>
            )}
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

/** This month's count, then the last few things it did on its own, each
 *  saying what came of it. */
export function DoneForYou() {
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
    <PreviewCard title="Done for you" href="/memories?tab=activity" linkLabel="All activity">
      <MonthCountLine />
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
            // A finished run says what came of it (and links its write-up).
            const detail =
              it.kind === "workflow_done" || it.kind === "contacts_kept"
                ? (it.detail ?? undefined)
                : it.kind === "action"
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
                href={writeUpHref(it)}
                linkLabel={writeUpLabel(it)}
              />
            );
          })}
        </ul>
      )}
    </PreviewCard>
  );
}

/** A focused screen behind the overview: a way back, a title, one job. */
export function FocusedScreen({
  title,
  description,
  backHref = "/memories",
  children,
}: {
  title: string;
  description: string;
  /** The tab it was opened from. */
  backHref?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-5">
      <header>
        <Link
          href={backHref}
          className="inline-flex h-10 items-center gap-1 rounded-xl px-2 -ml-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
        >
          <Icon name="arrow-left" size="w-4 h-4" />
          Memory
        </Link>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg mt-1">{title}</h1>
        <p className="text-[15px] text-fg-muted mt-1.5 max-w-2xl">{description}</p>
      </header>
      {children}
    </div>
  );
}
