"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import Icon from "@/components/Icon";
import Button from "@/components/ui/Button";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
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
import {
  DeferredForget,
  fixedMissIds,
  groupByKind,
  learningStats,
  toggleAll,
  unhelpfulLessonIds,
  type MissKind,
} from "@/lib/learning";

import { EmptyState, STAT_VALUE_TONE, formatDate, formatRunAt, type StatTone } from "./shared";

// Learning (the Reflection screen). At the top, this week's numbers, with how
// it learns one tap away. After a chat goes quiet, the conversation review
// grades each turn from the person's own words, the reply and the signals the
// product recorded (a 👎, a stopped reply, a turn taken back, a claim no tool
// backed, a failed tool). The misses land here with what the person wanted
// instead, so the owner can see where the Executive falls short in this
// workspace. Each person sees their own; the owner also the ones from
// channels and group chats (never a teammate's own chat).
// Beside the misses, what it learned from them: one-line lessons in the
// person's own words that reach later turns, with how those turns went.
// A lesson that doesn't help retires on its own; Forget retires it by hand.
// At night the Executive replays the misses with today's lessons, in a dry
// run nobody receives, and a judge says whether each would now go right:
// that is the "would go right today" number, and the note on each miss.
// The list is long, so it is split by category, a row opens for its detail,
// and several can be forgotten at once. Forgetting waits a few seconds for
// Undo before it is sent, since a forgotten miss is deleted.
// Nothing here notifies anyone: it is a record, opened on purpose.

type View = "misses" | "lessons";
type Category = "all" | MissKind;

const EMPTY =
  "Nothing yet. After a chat goes quiet, the Executive grades its own answers; the ones that fell short show up here.";

const KIND_LABEL: Record<MissKind, string> = {
  company: "Something to learn",
  product: "Couldn't do it",
};

const RETIRED_LABEL: Record<NonNullable<ReflectionLesson["retired_reason"]>, string> = {
  unhelpful: "stopped: the chats it reached went worse",
  forgotten: "stopped: forgotten",
  room: "stopped: made room for newer ones",
  replay: "stopped: replaying showed it didn't help",
};

/** How long a forget waits for Undo before it is sent. */
const UNDO_MS = 6000;

/** One forgotten row, waiting for Undo. */
interface Forget {
  view: View;
  id: number;
}

const keyOf = (f: Forget) => `${f.view === "misses" ? "m" : "l"}:${f.id}`;

function sendForget(f: Forget, keepalive: boolean): Promise<void> {
  return f.view === "misses" ? forgetReflectionMiss(f.id, { keepalive }) : forgetReflectionLesson(f.id, { keepalive });
}

export default function ReflectionTab() {
  const [state, setState] = useState<ReflectionState | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  const [view, setView] = useState<View>("misses");
  const [category, setCategory] = useState<Category>("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [showRetired, setShowRetired] = useState(false);
  // Forgotten here but not yet sent ("m:<id>" / "l:<id>"), and the toast.
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [toast, setToast] = useState<string | null>(null);
  const [queue] = useState(() => new DeferredForget<Forget>(sendForget, UNDO_MS));
  // A forget sent after leaving the screen (unmount, page close) reports nothing.
  const mounted = useRef(true);
  const tabsId = useId();

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

  // Leaving the screen sends what is waiting rather than dropping it: on
  // unmount, and when the tab closes or reloads (keepalive, since the page
  // is going away and nothing will read the answer).
  useEffect(() => {
    mounted.current = true;
    const onPageHide = () => void queue.flush({ keepalive: true });
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      mounted.current = false;
      void queue.flush();
    };
  }, [queue]);

  const pick = useCallback((next: { view?: View; category?: Category }) => {
    if (next.view !== undefined) setView(next.view);
    if (next.category !== undefined) setCategory(next.category);
    setSelected(new Set());
    setOpen(new Set());
  }, []);

  const forgetSelected = useCallback(() => {
    if (selected.size === 0) return;
    const items = [...selected].map((id) => ({ view, id }));
    setHidden((h) => new Set([...h, ...items.map(keyOf)]));
    setSelected(new Set());
    const n = items.length;
    setToast(view === "misses" ? `Forgot ${n}.` : `Stopped saying ${n === 1 ? "it" : n}.`);
    queue.hold(items, (sent, failedSends) => {
      if (!mounted.current) return;
      // Drop the toast unless a newer forget is waiting, then reload.
      setToast((t) => (queue.waiting.length ? t : null));
      if (failedSends > 0) window.alert("Some of them couldn't be forgotten.");
      void refresh().then(() => {
        const done = new Set(sent.map(keyOf));
        setHidden((h) => new Set([...h].filter((k) => !done.has(k))));
      });
    });
  }, [queue, refresh, selected, view]);

  const undo = useCallback(() => {
    const back = new Set(queue.undo().map(keyOf));
    setHidden((h) => new Set([...h].filter((k) => !back.has(k))));
    setToast(null);
  }, [queue]);

  if (failed) return <Card><EmptyState message="This is unavailable right now." /></Card>;
  if (state === undefined) return <div className="text-fg-muted text-[15px] py-4">Loading…</div>;
  if (state === null) {
    return (
      <Card>
        <EmptyState message="There is nothing for you here. It's kept for people on the People list, once they're signed in." />
      </Card>
    );
  }

  const { weeks, replay, sees_everyone: seesEveryone } = state;
  const misses = state.misses.filter((m) => !hidden.has(`m:${m.id}`));
  const lessons = state.lessons.filter((l) => !hidden.has(`l:${l.id}`));
  const live = lessons.filter((l) => l.retired_at === null);
  const retired = lessons.filter((l) => l.retired_at !== null);
  const kinds = groupByKind(misses);
  // A chip whose misses are all gone (forgotten, or a refresh) falls back to
  // All, so the other kind stays in reach.
  const cat: Category = category !== "all" && !kinds.some((g) => g.kind === category) ? "all" : category;
  const shownMisses = cat === "all" ? misses : misses.filter((m) => m.kind === cat);
  const shownIds = view === "misses" ? shownMisses.map((m) => m.id) : live.map((l) => l.id);
  const quick = view === "misses" ? fixedMissIds(shownMisses) : unhelpfulLessonIds(live);
  const allSelected = shownIds.length > 0 && shownIds.every((id) => selected.has(id));
  const toggle = (id: number) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const toggleOpen = (id: number) =>
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="space-y-5">
      <ThisWeek weeks={weeks} liveLessons={live.length} replay={replay} />

      <div className="space-y-3">
        <SectionTabs<View>
          label="Show"
          idBase={tabsId}
          active={view}
          onChange={(v) => pick({ view: v, category: "all" })}
          tabs={[
            { id: "misses", label: "What fell short", count: misses.length },
            { id: "lessons", label: "What it learned", count: live.length },
          ]}
        />
        {view === "misses" && kinds.length > 1 && (
          <div role="group" aria-label="Category" className="flex flex-wrap gap-2">
            {(["all", ...kinds.map((g) => g.kind)] as Category[]).map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => pick({ category: c })}
                aria-pressed={cat === c}
                className={`h-10 text-sm px-3.5 rounded-xl border transition-colors ${
                  cat === c
                    ? "bg-accent/10 text-accent border-accent/30"
                    : "bg-surface-overlay/40 text-fg-muted border-line hover:text-fg"
                }`}
              >
                {c === "all" ? "All" : KIND_LABEL[c]}
              </button>
            ))}
          </div>
        )}
      </div>

      <Card {...sectionPanelProps(tabsId, view)}>
        {shownIds.length === 0 && (view === "misses" || retired.length === 0) ? (
          <EmptyState message={view === "misses" ? EMPTY : "No lessons yet. They come from the misses."} />
        ) : (
          <>
            {shownIds.length > 0 && (
              <div className="flex items-center justify-between gap-3 py-1.5 border-b border-line">
                <label className="flex items-center gap-3 min-h-[44px] text-sm text-fg-muted cursor-pointer">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={() => setSelected((s) => toggleAll(s, shownIds))}
                    className="h-[18px] w-[18px] accent-accent-strong"
                  />
                  Select all
                </label>
                {quick.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setSelected(new Set(quick))}
                    className="min-h-[40px] text-sm font-semibold text-accent hover:underline"
                  >
                    {view === "misses"
                      ? `Select the ${quick.length} that ${quick.length === 1 ? "is" : "are"} fixed`
                      : `Select the ${quick.length} not helping`}
                  </button>
                )}
              </div>
            )}
            {view === "misses" &&
              (cat === "all" ? kinds : [{ kind: cat, items: shownMisses }]).map((g) => (
                <div key={g.kind} className="pb-2">
                  {cat === "all" && <GroupHeading title={KIND_LABEL[g.kind]} count={g.items.length} />}
                  {g.items.map((m, i) => (
                    <MissRow
                      key={m.id}
                      miss={m}
                      first={i === 0}
                      seesEveryone={seesEveryone}
                      selected={selected.has(m.id)}
                      onToggle={() => toggle(m.id)}
                      expanded={open.has(m.id)}
                      onExpand={() => toggleOpen(m.id)}
                    />
                  ))}
                </div>
              ))}
            {view === "lessons" && (
              <div className="pb-2">
                {live.length > 0 && seesEveryone && (
                  <p className="text-sm text-fg-muted pt-3">
                    A lesson you taught it in a channel or group chat reaches everyone&apos;s chats.
                  </p>
                )}
                {live.map((l, i) => (
                  <LessonRow
                    key={l.id}
                    lesson={l}
                    first={i === 0}
                    seesEveryone={seesEveryone}
                    selected={selected.has(l.id)}
                    onToggle={() => toggle(l.id)}
                    expanded={open.has(l.id)}
                    onExpand={() => toggleOpen(l.id)}
                  />
                ))}
                {retired.length > 0 && (
                  <>
                    <button
                      type="button"
                      onClick={() => setShowRetired((v) => !v)}
                      aria-expanded={showRetired}
                      className="mt-1 min-h-[44px] text-sm text-fg-muted hover:text-fg"
                    >
                      {showRetired ? "Hide stopped lessons" : `Show ${retired.length} stopped`}
                    </button>
                    {showRetired && (
                      <div>
                        <GroupHeading title="Stopped saying it" count={retired.length} />
                        {retired.map((l, i) => (
                          <LessonRow
                            key={l.id}
                            lesson={l}
                            first={i === 0}
                            seesEveryone={seesEveryone}
                            expanded={open.has(l.id)}
                            onExpand={() => toggleOpen(l.id)}
                          />
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </>
        )}
      </Card>

      {(selected.size > 0 || toast) && (
        <div className="sticky bottom-4 z-10 flex flex-col items-start gap-2">
          {toast && (
            <div
              role="status"
              className="flex max-w-full items-center gap-2 rounded-2xl border border-line-strong bg-surface-overlay py-1.5 pl-5 pr-1.5 shadow-lg"
            >
              <span className="text-sm text-fg">{toast}</span>
              <Button variant="ghost" size="sm" className="!text-accent" onClick={undo}>
                Undo
              </Button>
            </div>
          )}
          {selected.size > 0 && (
            <div
              role="region"
              aria-label="Selection"
              className="flex w-full items-center gap-2 rounded-2xl border border-line-strong bg-surface-overlay py-2 pl-5 pr-2 shadow-lg"
            >
              <span className="flex-1 text-[15px] font-semibold text-fg">{selected.size} selected</span>
              <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                Cancel
              </Button>
              <Button variant="danger" size="sm" onClick={forgetSelected}>
                {view === "misses" ? `Forget ${selected.size}` : `Stop saying ${selected.size}`}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Card({ children, ...rest }: { children: React.ReactNode } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...rest} className="rounded-2xl border border-line bg-surface-elevated px-4 sm:px-5 py-1">
      {children}
    </div>
  );
}

function GroupHeading({ title, count }: { title: string; count: number }) {
  return (
    <h3 className="flex items-baseline gap-2 pt-5 pb-1 text-base font-semibold text-fg">
      {title}
      <span className="text-sm font-normal text-fg-subtle tabular-nums">{count}</span>
    </h3>
  );
}

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

/** This week's numbers, with the four steps of how it learns behind a link. */
function ThisWeek({
  weeks,
  liveLessons,
  replay,
}: {
  weeks: ReflectionWeek[];
  liveLessons: number;
  replay: ReflectionReplay;
}) {
  const [how, setHow] = useState(false);
  const stats = learningStats(weeks, liveLessons, replay);
  const lastRun = replay.last_run_at ? formatRunAt(replay.last_run_at).absolute : null;
  const numbers: { value: string; label: string; tone: StatTone }[] = [
    { value: String(stats.checked), label: "answers checked", tone: "default" },
    { value: String(stats.fellShort), label: "fell short", tone: stats.fellShort > 0 ? "amber" : "default" },
    { value: String(stats.lessons), label: stats.lessons === 1 ? "lesson kept" : "lessons kept", tone: "accent" },
    {
      value: stats.wouldGoRight ? `${stats.wouldGoRight.pass} of ${stats.wouldGoRight.of}` : "–",
      label: stats.wouldGoRight ? "would go right today" : "nothing to recheck yet",
      tone: stats.wouldGoRight && stats.wouldGoRight.pass > 0 ? "emerald" : "default",
    },
  ];
  return (
    <section aria-labelledby="learning-this-week" className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-6">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h2 id="learning-this-week" className="text-lg font-semibold text-fg">
          This week
        </h2>
        <button
          type="button"
          onClick={() => setHow((v) => !v)}
          aria-expanded={how}
          className="inline-flex h-10 items-center gap-1 rounded-xl px-2 -mr-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
        >
          How it learns
          <Icon name="chevron-right" size="w-4 h-4" className={how ? "rotate-90" : ""} />
        </button>
      </div>
      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-4 sm:gap-6">
        {numbers.map((n) => (
          <div key={n.label} className="min-w-0 flex flex-col-reverse">
            <dt className="text-sm text-fg-muted mt-1">{n.label}</dt>
            <dd className={`text-2xl sm:text-3xl font-bold tabular-nums leading-tight ${STAT_VALUE_TONE[n.tone]}`}>
              {n.value}
            </dd>
          </div>
        ))}
      </dl>
      {how && (
        <div className="mt-5 border-t border-line pt-5">
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
          <p className="text-sm text-fg-muted mt-3">
            Chats that read your mail are looked at for you alone.
            {lastRun ? ` Last checked ${lastRun}.` : " The first nightly check runs once something has gone wrong."}
            {replay.evidence > 0 &&
              ` ${replay.evidence} ${replay.evidence === 1 ? "depends" : "depend"} on what that day's calendar or inbox held, so ${replay.evidence === 1 ? "it is" : "they are"} kept as evidence only.`}
          </p>
        </div>
      )}
    </section>
  );
}

/** The miss's last replay: still wrong, would go right now, or evidence only. */
function replayLabel(miss: ReflectionMiss): string | null {
  if (miss.replay_state === "closed" && miss.closed_at) return `Would go right today (since ${formatDate(miss.closed_at)})`;
  if (miss.replay_state === "evidence") return "Can't be replayed: depends on that day";
  if (miss.replayed_at) return `Still goes wrong (replayed ${formatDate(miss.replayed_at)})`;
  return null;
}

/** One selectable row: a checkbox, then the line and its meta, which open the detail. */
function Row({
  first,
  title,
  meta,
  selected,
  onToggle,
  expanded,
  onExpand,
  children,
}: {
  first: boolean;
  title: string;
  meta: React.ReactNode;
  selected?: boolean;
  onToggle?: () => void;
  expanded: boolean;
  onExpand: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className={first ? "" : "border-t border-line"}>
      <div className="flex items-start gap-3">
        {onToggle ? (
          <label className="flex min-h-[44px] flex-none cursor-pointer items-start pt-[15px]">
            <input
              type="checkbox"
              checked={!!selected}
              onChange={onToggle}
              aria-label={`Select: ${title}`}
              className="h-[18px] w-[18px] accent-accent-strong"
            />
          </label>
        ) : (
          <span className="w-[18px] flex-none" />
        )}
        <button
          type="button"
          onClick={onExpand}
          aria-expanded={expanded}
          className="flex min-w-0 flex-1 flex-col gap-1 py-3.5 text-left"
        >
          <span className="text-[15px] font-medium leading-snug text-fg">{title}</span>
          <span className="text-sm text-fg-muted">{meta}</span>
        </button>
      </div>
      {expanded && <div className="ml-[30px] -mt-1 pb-3.5 space-y-1 text-sm text-fg-muted">{children}</div>}
    </div>
  );
}

function MissRow({
  miss,
  first,
  seesEveryone,
  selected,
  onToggle,
  expanded,
  onExpand,
}: {
  miss: ReflectionMiss;
  first: boolean;
  seesEveryone: boolean;
  selected: boolean;
  onToggle: () => void;
  expanded: boolean;
  onExpand: () => void;
}) {
  const replay = replayLabel(miss);
  return (
    <Row
      first={first}
      title={miss.message}
      selected={selected}
      onToggle={onToggle}
      expanded={expanded}
      onExpand={onExpand}
      meta={
        <>
          {formatDate(miss.created_at)}
          {seesEveryone && miss.person_name && ` · ${miss.person_name}`}
          {miss.replay_state === "closed" && <span className="text-emerald-500"> · would go right today</span>}
        </>
      }
    >
      <p>
        <span className="font-medium">It said: </span>
        {miss.reply}
      </p>
      {miss.wanted && (
        <p className="text-fg">
          <span className="font-medium text-fg-muted">They wanted: </span>
          {miss.wanted}
        </p>
      )}
      {(miss.signals.length > 0 || replay) && (
        <p title={miss.replay_notes || undefined}>{[...miss.signals, ...(replay ? [replay] : [])].join(" · ")}</p>
      )}
      <Link
        href={`/?session=${encodeURIComponent(miss.session_id)}`}
        className="inline-flex min-h-[40px] items-center font-semibold text-accent hover:underline"
      >
        See the conversation
      </Link>
    </Row>
  );
}

function LessonRow({
  lesson,
  first,
  seesEveryone,
  selected,
  onToggle,
  expanded,
  onExpand,
}: {
  lesson: ReflectionLesson;
  first: boolean;
  seesEveryone: boolean;
  selected?: boolean;
  onToggle?: () => void;
  expanded: boolean;
  onExpand: () => void;
}) {
  const reached = lesson.shown;
  const went =
    reached === 0
      ? "not reached a chat yet"
      : `reached ${reached} ${reached === 1 ? "chat" : "chats"}: ${lesson.fine_after} went fine, ${lesson.miss_after} fell short`;
  return (
    <Row
      first={first}
      title={lesson.text}
      selected={selected}
      onToggle={onToggle}
      expanded={expanded}
      onExpand={onExpand}
      meta={
        <>
          {formatDate(lesson.created_at)}
          {seesEveryone && lesson.person_name && ` · ${lesson.person_name}`}
          {` · ${lesson.retired_reason ? RETIRED_LABEL[lesson.retired_reason] : went}`}
          {seesEveryone && lesson.shared && " · reaches everyone"}
        </>
      }
    >
      <p title={lesson.quote}>
        <span className="font-medium">From: </span>&ldquo;{lesson.quote}&rdquo;
      </p>
      <Link
        href={`/?session=${encodeURIComponent(lesson.session_id)}`}
        className="inline-flex min-h-[40px] items-center font-semibold text-accent hover:underline"
      >
        See the conversation
      </Link>
    </Row>
  );
}
