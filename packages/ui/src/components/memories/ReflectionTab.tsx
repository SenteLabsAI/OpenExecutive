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
} from "@/lib/api";
import { DeferredForget, learningStats, nowRight, toggleAll, unhelpfulLessonIds } from "@/lib/learning";

import { EmptyState, STAT_VALUE_TONE, formatDate, formatRunAt, type StatTone } from "./shared";
import { PageSpinner } from "@/components/ui/Spinner";

// Learning (the Reflection screen). After a chat goes quiet, the conversation
// review grades each turn from the person's own words, the reply and the
// signals the product recorded (a 👎, a stopped reply, a turn taken back, a
// claim no tool backed, a failed tool). From what fell short it keeps
// one-line lessons in the person's own words that reach later turns, and at
// night it replays the misses with today's lessons, in a dry run nobody
// receives, where a judge says whether each would now go right.
// This screen shows only what came of that: the lessons in use, with how many
// chats they reached, and the misses the nightly recheck now gets right. What
// fell short is never listed or counted here; it feeds the lessons and the
// recheck. Each person sees their own; the owner also the ones from channels
// and group chats (never a teammate's own chat).
// A lesson that doesn't help retires on its own; Stop saying retires it by
// hand. Several rows can be cleared at once, and clearing waits a few
// seconds for Undo before it is sent, since a forgotten row is deleted.
// Nothing here notifies anyone: it is a record, opened on purpose.

type View = "lessons" | "misses";

const EMPTY_LESSONS = "No lessons yet. After a chat goes quiet, I look back at it and keep what I learn here.";
const EMPTY_FIXED = "Nothing here yet. When a nightly recheck shows I now get something right, it shows here.";

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
  const [view, setView] = useState<View>("lessons");
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

  const pick = useCallback((next: View) => {
    setView(next);
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
  if (state === undefined) return <PageSpinner />;
  if (state === null) {
    return (
      <Card>
        <EmptyState message="There is nothing for you here. It's kept for people on the People list, once they're signed in." />
      </Card>
    );
  }

  const { replay, sees_everyone: seesEveryone } = state;
  const misses = nowRight(state.misses.filter((m) => !hidden.has(`m:${m.id}`)));
  const lessons = state.lessons.filter((l) => !hidden.has(`l:${l.id}`));
  const live = lessons.filter((l) => l.retired_at === null);
  const retired = lessons.filter((l) => l.retired_at !== null);
  const shownIds = view === "misses" ? misses.map((m) => m.id) : live.map((l) => l.id);
  const quick = view === "misses" ? [] : unhelpfulLessonIds(live);
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
      <GettingBetter lessons={lessons} misses={misses} replay={replay} />

      <SectionTabs<View>
        label="Show"
        idBase={tabsId}
        active={view}
        onChange={pick}
        tabs={[
          { id: "lessons", label: "What I learned", count: live.length },
          { id: "misses", label: "Now gets right", count: misses.length },
        ]}
      />

      <Card {...sectionPanelProps(tabsId, view)}>
        {shownIds.length === 0 && (view === "misses" || retired.length === 0) ? (
          <EmptyState message={view === "misses" ? EMPTY_FIXED : EMPTY_LESSONS} />
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
                    {`Select the ${quick.length} not helping`}
                  </button>
                )}
              </div>
            )}
            {view === "misses" && (
              <div className="pb-2">
                {misses.map((m, i) => (
                  <FixedRow
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
            )}
            {view === "lessons" && (
              <div className="pb-2">
                {live.length > 0 && seesEveryone && (
                  <p className="text-sm text-fg-muted pt-3">
                    Each lesson applies only to the chats of the person it came from.
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
    body: "Once the chat goes quiet, it checks each answer. It finishes anything left undone and notes what it could do better.",
    when: "30 min after",
  },
  {
    title: "It keeps a lesson",
    body: "One line in your own words. It reads it the next time you ask about the same thing.",
    when: "Same pass",
  },
  {
    title: "It checks itself",
    body: "Each night it reruns those answers with what it has learned, in a dry run nobody receives. A lesson that doesn't help is dropped.",
    when: "Every night",
  },
];

/** What came of the learning so far, with the four steps of how it learns behind a link. */
function GettingBetter({
  lessons,
  misses,
  replay,
}: {
  lessons: ReflectionLesson[];
  misses: ReflectionMiss[];
  replay: ReflectionReplay;
}) {
  const [how, setHow] = useState(false);
  const stats = learningStats(lessons, misses);
  const lastRun = replay.last_run_at ? formatRunAt(replay.last_run_at).absolute : null;
  const numbers: { value: string; label: string; tone: StatTone }[] = [
    { value: String(stats.lessons), label: stats.lessons === 1 ? "lesson in use" : "lessons in use", tone: "accent" },
    { value: String(stats.uses), label: stats.uses === 1 ? "time used in a chat" : "times used in chats", tone: "default" },
    {
      value: String(stats.nowRight),
      label: stats.nowRight === 1 ? "thing I now get right" : "things I now get right",
      tone: stats.nowRight > 0 ? "emerald" : "default",
    },
  ];
  return (
    <section aria-labelledby="learning-getting-better" className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-6">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h2 id="learning-getting-better" className="text-lg font-semibold text-fg">
          Getting better
        </h2>
        <button
          type="button"
          onClick={() => setHow((v) => !v)}
          aria-expanded={how}
          className="inline-flex h-10 items-center gap-1 rounded-xl px-2 -mr-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
        >
          How I learn
          <Icon name="chevron-right" size="w-4 h-4" className={how ? "rotate-90" : ""} />
        </button>
      </div>
      <dl className="grid grid-cols-3 gap-4 sm:gap-6">
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
            {lastRun ? ` Last checked ${lastRun}.` : " The first nightly check hasn't run yet."}
          </p>
        </div>
      )}
    </section>
  );
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

/** A miss the nightly recheck now gets right: what was asked, and since when. */
function FixedRow({
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
          <span className="text-emerald-500">
            Gets it right{miss.closed_at ? ` since ${formatDate(miss.closed_at)}` : " now"}
          </span>
          {seesEveryone && miss.person_name && ` · ${miss.person_name}`}
        </>
      }
    >
      {miss.wanted && (
        <p className="text-fg">
          <span className="font-medium text-fg-muted">Wanted: </span>
          {miss.wanted}
        </p>
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
  // "not helping yet" is what "Select the N not helping" picks by.
  const went =
    reached === 0
      ? "not used yet"
      : `used in ${reached} ${reached === 1 ? "chat" : "chats"}${lesson.miss_after > lesson.fine_after ? " · not helping yet" : ""}`;
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
