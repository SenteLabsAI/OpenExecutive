"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import OverflowMenu from "@/components/ui/OverflowMenu";
import {
  forgetReflectionMiss,
  getReflection,
  type ReflectionMiss,
  type ReflectionState,
  type ReflectionWeek,
} from "@/lib/api";
import { EmptyState, formatDate } from "./shared";

// Reflection — what it got wrong. After a chat goes quiet, the conversation
// review grades each turn from the person's own words, the reply and the
// signals the product recorded (a 👎, a stopped reply, a turn taken back, a
// claim no tool backed, a failed tool). The misses land here with what the
// person wanted instead, so the owner can see where the Executive falls
// short in this workspace. Each person sees their own; the owner also the ones
// from channels and group chats (never a teammate's own chat).
// Nothing here notifies anyone: it is a record, opened on purpose.

const EMPTY =
  "Nothing yet. After a chat goes quiet, the Executive grades its own answers; the ones that fell short show up here.";

const KIND_LABEL: Record<ReflectionMiss["kind"], string> = {
  company: "Something to learn",
  product: "Couldn't do it",
};

const KIND_PILL: Record<ReflectionMiss["kind"], string> = {
  company: "bg-amber-500/15 text-amber-500 border-amber-500/30",
  product: "bg-sky-500/15 text-sky-500 border-sky-500/30",
};

/** "W42" from "2026-W42". */
function weekLabel(week: string): string {
  const i = week.indexOf("-W");
  return i >= 0 ? `W${week.slice(i + 2)}` : week;
}

export default function ReflectionTab() {
  const [state, setState] = useState<ReflectionState | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setState(await getReflection());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleForget = useCallback(
    async (miss: ReflectionMiss) => {
      if (!window.confirm(`Forget this one?\n\n${miss.message}`)) return;
      try {
        await forgetReflectionMiss(miss.id);
      } catch {
        window.alert("Failed to forget.");
        return;
      }
      void refresh();
    },
    [refresh],
  );

  if (failed) return <EmptyState message="This is unavailable right now." />;
  if (state === undefined) return <div className="text-fg-muted text-[15px] py-4">Loading…</div>;
  if (state === null) {
    return (
      <EmptyState message="There is nothing for you here. It's kept for people on the People list, once they're signed in." />
    );
  }

  const { misses, weeks, sees_everyone: seesEveryone } = state;
  const company = misses.filter((m) => m.kind === "company");
  const product = misses.filter((m) => m.kind === "product");

  return (
    <div className="space-y-4 py-3">
      <Trend weeks={weeks} />
      {misses.length === 0 ? (
        <EmptyState message={EMPTY} />
      ) : (
        <p className="text-sm text-fg-muted">
          {seesEveryone ? "Your chats and the shared ones, newest first." : "Your chats, newest first."} Each one links to the
          conversation. Forget one to drop it from this record.
        </p>
      )}
      {company.length > 0 && (
        <Group
          title="Something to learn"
          blurb="A term, a fact, a preference or the right specialist would have fixed these."
          misses={company}
          seesEveryone={seesEveryone}
          onForget={handleForget}
        />
      )}
      {product.length > 0 && (
        <Group
          title="Couldn't do it"
          blurb="A tool failed, or it was asked for something it can't do yet."
          misses={product}
          seesEveryone={seesEveryone}
          onForget={handleForget}
        />
      )}
    </div>
  );
}

/** Graded turns and misses by week, oldest left, with the share as a bar. */
function Trend({ weeks }: { weeks: ReflectionWeek[] }) {
  if (weeks.length === 0) return null;
  const ordered = [...weeks].reverse().slice(-12);
  const graded = ordered.reduce((n, w) => n + w.graded, 0);
  const misses = ordered.reduce((n, w) => n + w.misses, 0);
  const share = graded ? Math.round((misses / graded) * 100) : 0;
  return (
    <section
      aria-label="Misses by week"
      className="rounded-xl border border-line bg-surface px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-3"
    >
      <div className="min-w-0 sm:w-56">
        <div className="text-2xl font-bold tabular-nums text-fg leading-tight">
          {misses} <span className="text-base font-semibold text-fg-muted">of {graded}</span>
        </div>
        <div className="text-sm text-fg-muted">
          turns fell short over {ordered.length === 1 ? "the last week" : `the last ${ordered.length} weeks`} ({share}%)
        </div>
      </div>
      <ol className="flex items-end gap-1.5 h-14 flex-1" aria-hidden="true">
        {ordered.map((w) => {
          const pct = w.graded ? Math.round((w.misses / w.graded) * 100) : 0;
          return (
            <li
              key={w.week}
              title={`${weekLabel(w.week)}: ${w.misses} of ${w.graded}`}
              className="flex-1 flex flex-col items-center justify-end gap-1 min-w-0"
            >
              <div
                className="w-full max-w-6 rounded-t bg-amber-500/60"
                style={{ height: `${Math.max(4, pct)}%` }}
              />
              <span className="text-[10px] text-fg-muted tabular-nums truncate">{weekLabel(w.week)}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Group({
  title,
  blurb,
  misses,
  seesEveryone,
  onForget,
}: {
  title: string;
  blurb: string;
  misses: ReflectionMiss[];
  seesEveryone: boolean;
  onForget: (miss: ReflectionMiss) => void;
}) {
  return (
    <section>
      <div className="text-base font-semibold text-fg">{title}</div>
      <p className="text-sm text-fg-muted mb-1">{blurb}</p>
      <div className="divide-y divide-line">
        {misses.map((m) => (
          <MissRow key={m.id} miss={m} seesEveryone={seesEveryone} onForget={() => onForget(m)} />
        ))}
      </div>
    </section>
  );
}

function MissRow({
  miss,
  seesEveryone,
  onForget,
}: {
  miss: ReflectionMiss;
  seesEveryone: boolean;
  onForget: () => void;
}) {
  return (
    <div className="py-3 flex gap-3">
      <div className="flex-1 min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          <span
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold ${KIND_PILL[miss.kind]}`}
          >
            {KIND_LABEL[miss.kind]}
          </span>
          <span>{formatDate(miss.created_at)}</span>
          {seesEveryone && miss.person_name && <span>· {miss.person_name}</span>}
          {miss.signals.map((s) => (
            <span key={s} className="rounded-full bg-surface-input/60 px-2 py-0.5">
              {s}
            </span>
          ))}
        </div>
        <div className="text-[15px] text-fg">
          <span className="text-fg-muted">They asked: </span>
          {miss.message}
        </div>
        <div className="text-sm text-fg-muted line-clamp-2" title={miss.reply}>
          <span className="font-medium">It said: </span>
          {miss.reply}
        </div>
        {miss.wanted && (
          <div className="text-[15px] text-fg">
            <span className="text-fg-muted">They wanted: </span>
            {miss.wanted}
          </div>
        )}
        <Link
          href={`/?session=${encodeURIComponent(miss.session_id)}`}
          className="inline-flex min-h-[40px] items-center text-sm font-medium text-accent hover:underline"
        >
          See the conversation
        </Link>
      </div>
      <OverflowMenu label="Options for this miss" items={[{ label: "Forget", onSelect: onForget, danger: true }]} />
    </div>
  );
}
