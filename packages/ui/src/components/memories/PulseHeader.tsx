"use client";

import { useEffect, useId, useMemo, useState } from "react";
import {
  getActivityDaily,
  listAdvice,
  listDecisions,
  listInitiatives,
  listPeopleMemory,
  listScheduledActions,
  type DailyActivityCount,
  type ScheduledAction,
  type WorkspaceMode,
} from "@/lib/api";
import Icon from "@/components/Icon";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  deriveVitals,
  formatTrend,
  formatVitalDate,
  type TrendDirection,
} from "@/lib/heartbeatVitals";
import {
  LivePulse,
  STAT_VALUE_TONE,
  Skeleton,
  StatTile,
  formatRunAt,
  groupByRhythm,
  metaFor,
  type StatTone,
} from "./shared";

// The Pulse page's at-a-glance numbers and its "heartbeat" — a
// GitHub-contributions-style heatmap of the Executive's self-initiated
// activity over the last 90 days. Up front sit three headline numbers (beats
// today, next beat, memories); the rest (rhythm counts and the vitals derived
// from the heatmap: streaks, totals, trend) sit under "More stats". Every
// metric is derived from data the page already needs (pending scheduled
// actions + the three memory lists); only the per-day heatmap requires its own
// endpoint, since /today/activity returns the last-N items, not a daily
// timeline. Peer memory is optional and remote, so it is fetched apart from
// the gating batch: the numbers render from local data and the Memories tile
// folds the peer notes in when (and only if) they arrive.

const HEATMAP_DAYS = 90;

interface HeaderData {
  pending: ScheduledAction[];
  memoriesTotal: number;
  /** Initiatives with status "active" — solo's stand-in for dept check-ins. */
  activeProjects: number;
  heatmap: DailyActivityCount[];
}

export interface PulseData {
  data: HeaderData | null;
  loading: boolean;
  /** Headline numbers first, then the ones under "More stats". */
  stats: { headline: Stat[]; more: Stat[] } | null;
}

/** Loads everything the Pulse numbers and heatmap need, once per page. */
export function usePulseData(): PulseData {
  const { mode } = useWorkspace();
  const [data, setData] = useState<HeaderData | null>(null);
  const [loading, setLoading] = useState(true);
  // null until peer memory answers (or fails / is disabled): the tile then
  // shows the local counts alone, which is not the same as "zero peer notes".
  const [peerNotes, setPeerNotes] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    listPeopleMemory()
      .then((people) => {
        if (!cancelled && people.status === "ok") setPeerNotes(people.conclusion_total);
      })
      .catch(() => {
        /* the tile simply shows the local counts */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);

    Promise.all([
      listScheduledActions("pending", 200, controller.signal),
      listDecisions(),
      listInitiatives(),
      listAdvice(),
      getActivityDaily(HEATMAP_DAYS, controller.signal),
    ])
      .then(([pending, decisions, initiatives, advice, daily]) => {
        if (cancelled) return;
        setData({
          pending,
          memoriesTotal: decisions.length + initiatives.length + advice.length,
          activeProjects: initiatives.filter((i) => i.status === "active").length,
          heatmap: daily.days,
        });
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError" && !cancelled) setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  const stats = useMemo(
    () => (data ? deriveStats(data, peerNotes, mode) : null),
    [data, peerNotes, mode],
  );
  return { data, loading, stats };
}

/** The three headline numbers, with the rest folded under "More stats". */
export function PulseSummary({ pulse }: { pulse: PulseData }) {
  const { data, loading, stats } = pulse;
  const [open, setOpen] = useState(false);
  const moreId = useId();

  return (
    <section aria-label="At a glance" className="space-y-3">
      <div className="grid grid-cols-3 gap-2 sm:gap-4">
        {loading || !stats
          ? Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="rounded-2xl border border-line bg-surface-elevated px-4 py-4 sm:px-5">
                <Skeleton className="h-4 w-20" />
                <Skeleton className="h-8 w-12 mt-2" />
              </div>
            ))
          : stats.headline.map((s) => (
              <StatTile key={s.label} label={s.label} value={s.value} hint={s.hint} tone={s.tone} />
            ))}
      </div>

      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={moreId}
        className="inline-flex h-10 items-center gap-1.5 rounded-xl px-2 text-sm font-medium text-fg-muted hover:text-fg hover:bg-surface-overlay transition-colors"
      >
        <Icon
          name="chevron-right"
          size="w-4 h-4"
          className={`transition-transform ${open ? "rotate-90" : ""}`}
        />
        {open ? "Fewer stats" : "More stats"}
      </button>

      {open && (
        <div id={moreId} className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5 space-y-5">
          <div className={VITALS_GRID}>
            {loading || !stats
              ? Array.from({ length: 3 }).map((_, i) => <VitalSkeleton key={i} />)
              : stats.more.map((s) => (
                  <Vital key={s.label} label={s.label} value={String(s.value)} hint={s.hint} tone={s.tone} />
                ))}
          </div>
          <div className="border-t border-line pt-5">
            <h3 className="text-sm font-semibold text-fg mb-3">
              Heartbeat, last {HEATMAP_DAYS} days
            </h3>
            {loading || !data ? <VitalsSkeleton /> : <Vitals days={data.heatmap} />}
          </div>
        </div>
      )}
    </section>
  );
}

/** The heatmap card at the top of the Heartbeat tab. */
export function HeartbeatCard({ pulse }: { pulse: PulseData }) {
  const { data, loading } = pulse;
  const vitals = useMemo(() => (data ? deriveVitals(data.heatmap) : null), [data]);
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="flex items-center justify-between gap-3 mb-4">
        <div className="flex items-baseline gap-2 flex-wrap">
          <h2 className="text-base font-semibold text-fg">Heartbeat</h2>
          <span className="text-sm text-fg-subtle">last {HEATMAP_DAYS} days</span>
        </div>
        <LivePulse />
      </div>
      <div className="flex flex-col md:flex-row md:items-center gap-5 md:gap-8">
        <div className="shrink-0 min-w-0">
          {loading || !data ? <Skeleton className="h-32 w-60" /> : <Heatmap days={data.heatmap} />}
        </div>
        {vitals && data && (
          <p className="text-[15px] text-fg-muted leading-relaxed">
            <span className="text-fg font-semibold">{plural(vitals.total, "beat")}</span> on{" "}
            {vitals.activeDays} of {data.heatmap.length} days.
            <br />
            Current streak:{" "}
            <span className="text-fg font-semibold">{plural(vitals.currentStreak.days, "day")}</span>
          </p>
        )}
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------- #
// Stat derivation
// --------------------------------------------------------------------------- #

export interface Stat {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "default" | "accent" | "emerald" | "amber";
}

function deriveStats(
  { pending, memoriesTotal, activeProjects, heatmap }: HeaderData,
  peerNotes: number | null,
  mode: WorkspaceMode,
): { headline: Stat[]; more: Stat[] } {
  const groups = groupByRhythm(pending);
  const followups = pending.filter((a) => a.kind === "ad_hoc").length;

  // Soonest pending fire = the literal "next beat".
  const soonest = pending.reduce<ScheduledAction | null>((best, a) => {
    if (!best) return a;
    return a.run_at.localeCompare(best.run_at) < 0 ? a : best;
  }, null);
  const nextBeat = soonest
    ? { value: formatRunAt(soonest.run_at).relative || "soon", hint: metaFor(soonest).label }
    : { value: "—", hint: "nothing scheduled" };

  // The heatmap is oldest → newest, so the last entry is today.
  const beatsToday = heatmap.length > 0 ? heatmap[heatmap.length - 1].count : 0;

  // Three episodic lists plus what peer memory has learned about people;
  // the hint says how much of the number is peer notes so it reconciles
  // with the tabs (the People tab badge counts people, not notes).
  const memories: Stat = {
    label: "Memories",
    value: memoriesTotal + (peerNotes ?? 0),
    hint:
      peerNotes === null
        ? undefined
        : peerNotes > 0
          ? `incl. ${peerNotes} peer notes`
          : "no peer notes yet",
  };

  return {
    headline: [
      { label: "Beats today", value: beatsToday, tone: "emerald", hint: "actions fired" },
      { label: "Next beat", value: nextBeat.value, tone: "accent", hint: nextBeat.hint },
      memories,
    ],
    more: [
      { label: "Daily rhythms", value: groups.daily.length },
      // Solo has no department check-ins; the projects it tracks take the slot.
      mode === "solo"
        ? { label: "Projects", value: activeProjects, hint: "active" }
        : { label: "Dept check-ins", value: groups.departments.length },
      { label: "Follow-ups", value: followups },
    ],
  };
}

// --------------------------------------------------------------------------- #
// Vitals — summary numbers beside the heatmap, derived from the same counts
// --------------------------------------------------------------------------- #

const VITALS_COUNT = 6;

const TREND_TONE: Record<TrendDirection, StatTone> = {
  up: "emerald",
  down: "amber",
  flat: "default",
  new: "emerald",
  none: "default",
};

function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function Vital({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: StatTone;
}) {
  return (
    <div className="min-w-0">
      <div className="text-sm font-medium text-fg-muted truncate">{label}</div>
      <div className={`mt-1 text-xl font-bold tabular-nums truncate ${STAT_VALUE_TONE[tone]}`}>
        {value}
      </div>
      {hint && <div className="text-sm text-fg-subtle mt-0.5 truncate">{hint}</div>}
    </div>
  );
}

const VITALS_GRID = "grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-5";

function Vitals({ days }: { days: DailyActivityCount[] }) {
  const v = useMemo(() => deriveVitals(days), [days]);
  const { trend } = v;

  return (
    <div className={VITALS_GRID}>
      <Vital
        label="Current streak"
        value={plural(v.currentStreak.days, "day")}
        hint={
          v.currentStreak.days === 0
            ? "no active days lately"
            : v.currentStreak.throughToday
              ? "through today"
              : "through yesterday"
        }
        tone="emerald"
      />
      <Vital label="Longest streak" value={plural(v.longestStreak, "day")} />
      <Vital
        label={`${days.length}-day total`}
        value={plural(v.total, "beat")}
        hint={`on ${v.activeDays} of ${days.length} days`}
      />
      <Vital
        label="Avg per active day"
        value={v.avgPerActiveDay === null ? "—" : v.avgPerActiveDay.toFixed(1)}
      />
      <Vital
        label="Busiest day"
        value={v.busiestDay ? formatVitalDate(v.busiestDay.date) : "—"}
        hint={v.busiestDay ? plural(v.busiestDay.count, "action") : undefined}
      />
      <Vital
        label={`Last ${trend.windowDays} vs prior`}
        value={formatTrend(trend)}
        hint={trend.windowDays > 0 ? `${trend.current} vs ${trend.prior}` : undefined}
        tone={TREND_TONE[trend.direction]}
      />
    </div>
  );
}

function VitalSkeleton() {
  return (
    <div>
      <Skeleton className="h-3 w-20" />
      <Skeleton className="h-6 w-14 mt-2" />
    </div>
  );
}

function VitalsSkeleton() {
  return (
    <div className={VITALS_GRID}>
      {Array.from({ length: VITALS_COUNT }).map((_, i) => (
        <VitalSkeleton key={i} />
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- #
// Heatmap — GitHub-contributions-style grid (weeks as columns, weekdays as rows)
// --------------------------------------------------------------------------- #

// Intensity → background. Step 0 is an idle cell (surface), 1–4 ramp emerald.
// These read correctly in both light and dark mode.
const LEVEL_BG = [
  "bg-surface-input/60",
  "bg-emerald-500/30",
  "bg-emerald-500/55",
  "bg-emerald-400/80",
  "bg-emerald-400",
];

// Inclusive upper bound of each non-max step; a day's count maps to the first
// step it fits under, else the top level. Length is LEVEL_BG.length − 1 so the
// two arrays stay in lockstep (steps 0..3 here, step 4 = "more than 6").
const INTENSITY_STOPS = [0, 1, 3, 6];

function intensity(count: number): number {
  for (let i = 0; i < INTENSITY_STOPS.length; i++) {
    if (count <= INTENSITY_STOPS[i]) return i;
  }
  return INTENSITY_STOPS.length; // top level (= LEVEL_BG.length − 1)
}

/** UTC weekday (0=Sun) of a YYYY-MM-DD date string. */
function weekday(date: string): number {
  return new Date(`${date}T00:00:00Z`).getUTCDay();
}

/** Chunk the dense day list into weekday-aligned columns of 7. */
function toWeeks(days: DailyActivityCount[]): (DailyActivityCount | null)[][] {
  if (days.length === 0) return [];
  const cells: (DailyActivityCount | null)[] = [];
  for (let i = 0; i < weekday(days[0].date); i++) cells.push(null); // lead padding
  cells.push(...days);
  while (cells.length % 7 !== 0) cells.push(null); // trailing padding
  const weeks: (DailyActivityCount | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

function Heatmap({ days }: { days: DailyActivityCount[] }) {
  const weeks = useMemo(() => toWeeks(days), [days]);
  const total = useMemo(() => days.reduce((n, d) => n + d.count, 0), [days]);

  return (
    <div>
      <div
        className="flex gap-1 overflow-x-auto pb-1"
        role="img"
        aria-label={`Activity heatmap: ${total} actions over the last ${days.length} days`}
      >
        {weeks.map((week, wi) => (
          <div key={wi} className="flex flex-col gap-1">
            {week.map((cell, di) =>
              cell === null ? (
                <div key={di} className="h-3.5 w-3.5" />
              ) : (
                <div
                  key={di}
                  className={`h-3.5 w-3.5 rounded-[4px] ${LEVEL_BG[intensity(cell.count)]}`}
                  title={`${cell.date}: ${cell.count} ${cell.count === 1 ? "action" : "actions"}`}
                  aria-label={`${cell.date}: ${cell.count} ${cell.count === 1 ? "action" : "actions"}`}
                />
              ),
            )}
          </div>
        ))}
      </div>

      {/* Legend */}
      <div className="flex items-center justify-end gap-1.5 mt-2 text-xs text-fg-subtle">
        <span>less</span>
        {LEVEL_BG.map((bg, i) => (
          <span key={i} className={`h-3 w-3 rounded-[3px] ${bg}`} aria-hidden="true" />
        ))}
        <span>more</span>
      </div>
    </div>
  );
}
