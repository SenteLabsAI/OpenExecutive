"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import OverflowMenu from "@/components/ui/OverflowMenu";
import {
  forgetReflectionLesson,
  forgetReflectionMiss,
  getReflection,
  type ReflectionLesson,
  type ReflectionMiss,
  type ReflectionReplay,
  type ReflectionState,
  type ReflectionWeek,
} from "@/lib/api";
import { learningStats } from "@/lib/learning";

import { EmptyState, formatDate, formatRunAt } from "./shared";

// Learning (the Reflection screen). At the top, how it learns: four steps and
// this person's own numbers. After a chat goes quiet, the conversation
// review grades each turn from the person's own words, the reply and the
// signals the product recorded (a 👎, a stopped reply, a turn taken back, a
// claim no tool backed, a failed tool). The misses land here with what the
// person wanted instead, so the owner can see where the Executive falls
// short in this workspace. Each person sees their own; the owner also the ones
// from channels and group chats (never a teammate's own chat).
// Below the misses, what it learned from them: one-line lessons in the
// person's own words that reach later turns, with how those turns went.
// A lesson that doesn't help retires on its own; Forget retires it by hand.
// At night the Executive replays the misses with today's lessons, in a dry
// run nobody receives, and a judge says whether each would now go right:
// that is the "would go right today" number, and the pill on each miss.
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

  const handleForgetLesson = useCallback(
    async (lesson: ReflectionLesson) => {
      if (!window.confirm(`Stop saying this?\n\n${lesson.text}`)) return;
      try {
        await forgetReflectionLesson(lesson.id);
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

  const { misses, lessons, weeks, replay, sees_everyone: seesEveryone } = state;
  const company = misses.filter((m) => m.kind === "company");
  const product = misses.filter((m) => m.kind === "product");
  const live = lessons.filter((l) => l.retired_at === null);
  const retired = lessons.filter((l) => l.retired_at !== null);

  return (
    <div className="space-y-4 py-3">
      <HowItLearns weeks={weeks} liveLessons={live.length} replay={replay} />
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
      {lessons.length > 0 && (
        <section>
          <div className="text-base font-semibold text-fg">What it learned</div>
          <p className="text-sm text-fg-muted mb-1">
            One line each, in your own words, from the misses above. It reads them on your later chats about
            the same thing, and keeps count of how those went. One that doesn&apos;t help stops on its own.
            {seesEveryone && " A lesson you taught it in a channel or group chat reaches everyone's chats."}
          </p>
          <div className="divide-y divide-line">
            {live.map((l) => (
              <LessonRow key={l.id} lesson={l} seesEveryone={seesEveryone} onForget={() => handleForgetLesson(l)} />
            ))}
          </div>
          {retired.length > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer text-sm text-fg-muted min-h-[40px] flex items-center">
                Stopped saying it ({retired.length})
              </summary>
              <div className="divide-y divide-line">
                {retired.map((l) => (
                  <LessonRow key={l.id} lesson={l} seesEveryone={seesEveryone} />
                ))}
              </div>
            </details>
          )}
        </section>
      )}
    </div>
  );
}

const RETIRED_LABEL: Record<NonNullable<ReflectionLesson["retired_reason"]>, string> = {
  unhelpful: "stopped: the chats it reached went worse, not better",
  forgotten: "stopped: forgotten",
  room: "stopped: made room for newer ones",
  replay: "stopped: replaying the chats showed it didn't help",
};

const HIDE_KEY = "oe.learning.howItLearns.hidden";

const STEPS: { title: string; body: string; when?: string }[] = [
  {
    title: "You chat",
    body: "On the web, your phone or a chat app. Every chat counts. It notices when you correct it or ask again.",
  },
  {
    title: "It looks back",
    body: "Once the chat goes quiet, it checks each answer. It finishes anything left undone and notes what fell short.",
    when: "30 min after",
  },
  {
    title: "It keeps a lesson",
    body: "One line in your own words. It reads it the next time you ask about the same thing.",
    when: "Same pass",
  },
  {
    title: "It checks itself",
    body: "Each night it reruns what went wrong, in a dry run nobody receives. A lesson that doesn't help is dropped.",
    when: "Every night",
  },
];

function readHidden(): boolean {
  try {
    return window.localStorage.getItem(HIDE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeHidden(hidden: boolean): void {
  try {
    if (hidden) window.localStorage.setItem(HIDE_KEY, "1");
    else window.localStorage.removeItem(HIDE_KEY);
  } catch {
    // Not kept: the card shows again next time.
  }
}

/** The four steps of how it learns, with this person's own numbers under them. */
function HowItLearns({
  weeks,
  liveLessons,
  replay,
}: {
  weeks: ReflectionWeek[];
  liveLessons: number;
  replay: ReflectionReplay;
}) {
  // Mounted only once the screen's data has loaded in the browser, so the
  // stored choice can be read up front.
  const [hidden, setHidden] = useState(readHidden);
  const toggle = () => {
    writeHidden(!hidden);
    setHidden(!hidden);
  };
  const stats = learningStats(weeks, liveLessons, replay);
  const lastRun = replay.last_run_at ? formatRunAt(replay.last_run_at).absolute : null;

  if (hidden) {
    return (
      <button
        type="button"
        onClick={toggle}
        className="min-h-[44px] text-sm font-semibold text-accent hover:underline"
      >
        How it learns
      </button>
    );
  }
  return (
    <section aria-labelledby="how-it-learns" className="rounded-2xl border border-line bg-surface-panel p-4 sm:p-5">
      <div className="flex items-baseline justify-between gap-3 mb-4">
        <h2 id="how-it-learns" className="text-lg font-semibold text-fg">
          How it learns
        </h2>
        <button
          type="button"
          onClick={toggle}
          className="min-h-[44px] px-1 text-sm font-semibold text-accent hover:underline"
        >
          Hide
        </button>
      </div>
      <ol className="grid grid-cols-1 lg:grid-cols-4 gap-3">
        {STEPS.map((step, i) => (
          <li
            key={step.title}
            className="rounded-xl border border-line bg-surface p-4 grid grid-cols-[2.25rem_1fr] lg:block gap-x-3"
          >
            <div className="h-8 w-8 rounded-full bg-accent/15 text-accent font-bold flex items-center justify-center lg:mb-3">
              {i + 1}
            </div>
            <div>
              <div className="text-base font-semibold text-fg">{step.title}</div>
              <p className="text-sm text-fg-muted mt-1">{step.body}</p>
              {step.when && (
                <span className="inline-block mt-2 rounded-full bg-accent/15 px-2.5 py-0.5 text-xs font-semibold text-accent">
                  {step.when}
                </span>
              )}
            </div>
          </li>
        ))}
      </ol>
      <dl className="grid grid-cols-2 lg:grid-cols-4 gap-4 mt-4 border-t border-line pt-4">
        <Stat value={String(stats.checked)} label="answers checked this week" />
        <Stat value={String(stats.fellShort)} label="fell short" tone={stats.fellShort > 0 ? "amber" : undefined} />
        <Stat value={String(stats.lessons)} label={stats.lessons === 1 ? "lesson kept" : "lessons kept"} />
        <Stat
          value={stats.wouldGoRight ? `${stats.wouldGoRight.pass} of ${stats.wouldGoRight.of}` : "–"}
          label={stats.wouldGoRight ? "would go right today" : "nothing to recheck yet"}
          tone={stats.wouldGoRight && stats.wouldGoRight.pass > 0 ? "green" : undefined}
        />
      </dl>
      <p className="text-sm text-fg-muted mt-3">
        Chats that read your mail are looked at for you alone.
        {lastRun ? ` Last checked ${lastRun}.` : " The first nightly check runs once something has gone wrong."}
        {replay.evidence > 0 &&
          ` ${replay.evidence} ${replay.evidence === 1 ? "depends" : "depend"} on what that day's calendar or inbox held, so ${replay.evidence === 1 ? "it is" : "they are"} kept as evidence only.`}
      </p>
    </section>
  );
}

function Stat({ value, label, tone }: { value: string; label: string; tone?: "amber" | "green" }) {
  const color = tone === "amber" ? "text-amber-500" : tone === "green" ? "text-emerald-500" : "text-fg";
  return (
    <div>
      <dt className="sr-only">{label}</dt>
      <dd className={`text-2xl font-bold tabular-nums leading-tight ${color}`}>{value}</dd>
      <dd className="text-sm text-fg-muted">{label}</dd>
    </div>
  );
}

const REPLAY_PILL: Record<ReflectionMiss["replay_state"], string> = {
  open: "bg-surface-input/60 text-fg-muted",
  closed: "bg-emerald-500/15 text-emerald-500",
  evidence: "bg-surface-input/60 text-fg-muted",
};

/** The miss's last replay: still wrong, would go right now, or evidence only. */
function replayLabel(miss: ReflectionMiss): string | null {
  if (miss.replay_state === "closed" && miss.closed_at) return `Would go right today (since ${formatDate(miss.closed_at)})`;
  if (miss.replay_state === "evidence") return "Can't be replayed: depends on that day";
  if (miss.replayed_at) return `Still goes wrong (replayed ${formatDate(miss.replayed_at)})`;
  return null;
}

function LessonRow({
  lesson,
  seesEveryone,
  onForget,
}: {
  lesson: ReflectionLesson;
  seesEveryone: boolean;
  onForget?: () => void;
}) {
  const reached = lesson.shown;
  const went =
    reached === 0
      ? "Not reached a chat yet"
      : `Reached ${reached} ${reached === 1 ? "chat" : "chats"}: ${lesson.fine_after} went fine, ${lesson.miss_after} fell short`;
  return (
    <div className="py-3 flex gap-3">
      <div className="flex-1 min-w-0 space-y-1">
        <div className="text-[15px] text-fg">{lesson.text}</div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          <span>{formatDate(lesson.created_at)}</span>
          {seesEveryone && lesson.person_name && <span>· {lesson.person_name}</span>}
          <span>· {went}</span>
          {seesEveryone && lesson.shared && (
            <span className="rounded-full bg-surface-input/60 px-2 py-0.5">reaches everyone</span>
          )}
          {lesson.retired_reason && (
            <span className="rounded-full bg-surface-input/60 px-2 py-0.5">{RETIRED_LABEL[lesson.retired_reason]}</span>
          )}
        </div>
        <div className="text-sm text-fg-muted" title={lesson.quote}>
          <span className="font-medium">From: </span>&ldquo;{lesson.quote}&rdquo;
        </div>
      </div>
      {onForget && (
        <OverflowMenu label="Options for this lesson" items={[{ label: "Forget", onSelect: onForget, danger: true }]} />
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
          {replayLabel(miss) && (
            <span
              className={`rounded-full px-2 py-0.5 ${REPLAY_PILL[miss.replay_state]}`}
              title={miss.replay_notes || undefined}
            >
              {replayLabel(miss)}
            </span>
          )}
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
