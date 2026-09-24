// Shared helpers for the Pulse page (memory + cadence). Extracted from the
// former single-file EpisodicMemories component so the Cadence and Memory
// sections — and the redesigned PulseHeader — can reuse them without
// duplication. Holds three things: domain/format helpers, the scheduled-action
// "rhythm" taxonomy (used by both the header stat strip and CadenceSection),
// and a handful of presentational primitives (StatTile, LivePulse, etc.).

import Icon, { type IconName } from "@/components/Icon";
import type { ScheduledAction } from "@/lib/api";

export const DOMAINS = [
  "strategy",
  "finance",
  "hr",
  "legal",
  "operations",
  "marketing",
  "product",
  "board",
  "general",
];

export const STATUSES = ["active", "paused", "completed", "planned"];

/** ISO timestamp → YYYY-MM-DD. */
export function formatDate(iso: string): string {
  return iso.slice(0, 10);
}

/** ISO timestamp → an absolute string plus a coarse relative label ("in 5m", "3d ago"). */
export function formatRunAt(iso: string): { absolute: string; relative: string } {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { absolute: iso, relative: "" };
  const absolute = d.toLocaleString();
  const deltaMs = d.getTime() - Date.now();
  const abs = Math.abs(deltaMs);
  const mins = Math.round(abs / 60_000);
  const hours = Math.round(abs / 3_600_000);
  const days = Math.round(abs / 86_400_000);
  let unit: string;
  if (mins < 60) unit = `${mins}m`;
  else if (hours < 48) unit = `${hours}h`;
  else unit = `${days}d`;
  const relative = deltaMs >= 0 ? `in ${unit}` : `${unit} ago`;
  return { absolute, relative };
}

export const STATUS_PILL: Record<string, string> = {
  pending: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  running: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  done: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  failed: "bg-red-500/15 text-red-300 border-red-500/30",
  cancelled: "bg-surface-input/40 text-fg-muted border-line-strong/40",
};

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="text-center py-16 text-fg-muted text-sm">{message}</div>
  );
}

// ---------------------------------------------------------------------------
// Rhythm taxonomy — turns raw scheduled_actions `kind` strings into the human
// rhythm groups shown on the Pulse page. Shared by PulseHeader (counts) and
// CadenceSection (cards). `ad_hoc` is intentionally absent from the groups: the
// Follow-ups block owns it (its own status filter + cancel).
// ---------------------------------------------------------------------------

export type RhythmGroup = "daily" | "departments" | "awaiting" | "system";

export interface KindMeta {
  label: string;
  group: RhythmGroup;
  blurb?: string;
}

export const KIND_META: Record<string, KindMeta> = {
  principal_brief_morning: {
    label: "Morning brief",
    group: "daily",
    blurb: "Whole-company state, sent to you each morning.",
  },
  executive_reflection: {
    label: "Executive reflection",
    group: "daily",
    blurb: "Decides what to act on, just before the morning brief.",
  },
  principal_brief_eod: {
    label: "End-of-day digest",
    group: "daily",
    blurb: "What landed today and what's still open.",
  },
  dept_cadence: { label: "Check-in", group: "departments" },
  awaiting_human: { label: "Awaiting a human reply", group: "awaiting" },
  proactive_nudge: { label: "Nudge", group: "awaiting" },
  nudge_scan: {
    label: "Nudge scan",
    group: "system",
    blurb: "Chases stalled commitments and idle initiatives.",
  },
  external_monitor_scan: {
    label: "External monitor",
    group: "system",
    blurb: "Watches the watch list for material signals.",
  },
  watchlist_research_scan: {
    label: "Watchlist research",
    group: "system",
    blurb: "Periodic research pass over your watch list.",
  },
};

export function metaFor(action: ScheduledAction): KindMeta {
  const meta = KIND_META[action.kind];
  if (meta) return meta;
  // Unknown kind: keep it visible rather than dropping it. Internal-channel
  // rows are system plumbing; anything else is a pending commitment.
  return {
    label: action.kind || "Scheduled action",
    group: action.channel === "__internal__" ? "system" : "awaiting",
  };
}

/** Group pending rows (excluding ad_hoc) by rhythm group, each sorted soonest-first. */
export function groupByRhythm(
  actions: ScheduledAction[],
): Record<RhythmGroup, ScheduledAction[]> {
  const groups: Record<RhythmGroup, ScheduledAction[]> = {
    daily: [],
    departments: [],
    awaiting: [],
    system: [],
  };
  for (const a of actions) {
    if (a.kind === "ad_hoc") continue;
    groups[metaFor(a).group].push(a);
  }
  for (const key of Object.keys(groups) as RhythmGroup[]) {
    groups[key].sort((x, y) => x.run_at.localeCompare(y.run_at));
  }
  return groups;
}

// ---------------------------------------------------------------------------
// Presentational primitives — kept dependency-free and theme-token-driven so
// they render correctly in both light and dark mode.
// ---------------------------------------------------------------------------

export type StatTone = "default" | "accent" | "emerald" | "amber";

export const STAT_VALUE_TONE: Record<StatTone, string> = {
  default: "text-fg",
  accent: "text-indigo-300",
  emerald: "text-emerald-300",
  amber: "text-amber-300",
};

/** A single at-a-glance metric: small label, big number, optional hint. */
export function StatTile({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: StatTone;
}) {
  return (
    <div className="rounded-xl border border-line bg-surface-elevated px-4 py-3 min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-wide text-fg-subtle truncate">
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums truncate ${STAT_VALUE_TONE[tone]}`}>
        {value}
      </div>
      {hint && <div className="text-[11px] text-fg-muted mt-0.5 truncate">{hint}</div>}
    </div>
  );
}

/**
 * "Live" indicator: a beating emerald dot + optional label. The ping animation
 * is disabled under `prefers-reduced-motion` via Tailwind's `motion-reduce`
 * variant (SSR-safe — no JS hook, no hydration mismatch). The solid dot always
 * shows, so the indicator never disappears, it just stops animating.
 */
export function LivePulse({
  label = "Live",
  className = "",
}: {
  label?: string;
  className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span className="relative flex h-2 w-2" aria-hidden="true">
        <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75 animate-ping motion-reduce:hidden" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
      </span>
      {label && (
        <span className="text-[11px] font-medium uppercase tracking-wide text-emerald-300">
          {label}
        </span>
      )}
    </span>
  );
}

export type TagTone = "info" | "muted";

const TAG_TONE: Record<TagTone, string> = {
  info: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  muted: "bg-surface-input/40 text-fg-subtle border-line",
};

/** Small categorical pill (e.g. "Once a day · for you"). Reuses the app's pill recipe. */
export function Tag({ label, tone = "muted" }: { label: string; tone?: TagTone }) {
  return (
    <span className={`px-1.5 py-0.5 rounded border text-[10px] font-medium ${TAG_TONE[tone]}`}>
      {label}
    </span>
  );
}

/** Section header: optional icon, title, optional count badge, optional tag pill, optional subtitle. */
export function SectionHeading({
  title,
  count,
  icon,
  tag,
  tagTone = "muted",
  subtitle,
}: {
  title: string;
  count?: number;
  icon?: IconName;
  tag?: string;
  tagTone?: TagTone;
  subtitle?: string;
}) {
  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 flex-wrap">
        {icon && <Icon name={icon} size="w-4 h-4" className="text-fg-subtle" />}
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        {count != null && (
          <span className="text-xs font-normal tabular-nums text-fg-subtle">{count}</span>
        )}
        {tag && <Tag label={tag} tone={tagTone} />}
      </div>
      {subtitle && <p className="text-xs text-fg-muted mt-0.5">{subtitle}</p>}
    </div>
  );
}

/** Shimmer placeholder for >300ms loads. Honors reduced-motion. */
export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={`animate-pulse motion-reduce:animate-none rounded-md bg-surface-input/60 ${className}`}
      aria-hidden="true"
    />
  );
}
