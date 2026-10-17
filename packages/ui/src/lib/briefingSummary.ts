// The Home screen's wording: the greeting, the one-line summary that replaced
// the row of status pills, and the single status chip a collapsed proposal
// card keeps. Kept apart from the components so `npm test` can check it (see
// scripts/briefingSummary.test.mjs). No imports, so the test can load this
// under `node --experimental-strip-types`.

/** "Good morning" before noon, "Good afternoon" until 6pm, then "Good evening". */
export function greeting(firstName: string | undefined, now: Date = new Date()): string {
  const h = now.getHours();
  const part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
  const name = firstName?.trim();
  return name ? `Good ${part}, ${name}` : `Good ${part}`;
}

/** Where a summary phrase leads: a lane on the page, one of the tiles' side
 * panels, or another route. */
export type SummaryTarget =
  | { kind: "needsYou" }
  | { kind: "panel"; panel: "handled" | "people" | "departments" | "inFlight" | "monitoring" }
  | { kind: "href"; href: string };

export interface SummaryPart {
  text: string;
  target: SummaryTarget;
}

export interface SummaryCounts {
  needsYou: number;
  handledOvernight: number;
  peopleOverdue: number;
  peopleNeedReply: number;
  deptAtRisk: number;
  /** Solo only: at-risk + off-track goals, which have no tile on the page. */
  goalsAtRisk?: number;
  inFlight: number;
  monitoring: number;
}

const plural = (n: number, one: string, many: string) => (n === 1 ? one : many);

/** The summary under the greeting, as phrases in the order they are read:
 * what needs you first, then what the Executive did on its own (trust and
 * relief), then the rest by urgency. Zero counts are dropped; an empty list
 * means all clear. Each phrase opens the detail it counts. */
export function briefingSummary(c: SummaryCounts): SummaryPart[] {
  const parts: SummaryPart[] = [];
  if (c.needsYou > 0)
    parts.push({
      text: `${c.needsYou} ${plural(c.needsYou, "thing needs", "things need")} you`,
      target: { kind: "needsYou" },
    });
  if (c.handledOvernight > 0)
    parts.push({
      text: `The Executive handled ${c.handledOvernight} overnight`,
      target: { kind: "panel", panel: "handled" },
    });
  if (c.peopleOverdue > 0)
    parts.push({ text: `${c.peopleOverdue} overdue`, target: { kind: "panel", panel: "people" } });
  if (c.peopleNeedReply > 0)
    parts.push({
      text: `${c.peopleNeedReply} awaiting reply`,
      target: { kind: "panel", panel: "people" },
    });
  if (c.deptAtRisk > 0)
    parts.push({
      text: `${c.deptAtRisk} ${plural(c.deptAtRisk, "department", "departments")} at risk`,
      target: { kind: "panel", panel: "departments" },
    });
  if (c.goalsAtRisk && c.goalsAtRisk > 0)
    parts.push({
      text: `${c.goalsAtRisk} ${plural(c.goalsAtRisk, "goal", "goals")} at risk`,
      target: { kind: "href", href: "/goals" },
    });
  if (c.inFlight > 0)
    parts.push({ text: `${c.inFlight} under way`, target: { kind: "panel", panel: "inFlight" } });
  if (c.monitoring > 0)
    parts.push({
      text: `${c.monitoring} ${plural(c.monitoring, "signal", "signals")} monitored`,
      target: { kind: "panel", panel: "monitoring" },
    });
  return parts;
}

/** Compact age label ("3h", "9d") for a card chip; "" for an unparseable stamp. */
export function ageLabel(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const mins = Math.max(0, Math.round((now.getTime() - t) / 60000));
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** Whole days between now and an ISO stamp: 0 = due within the day, negative =
 * past (floor, so an 11-hour-old deadline is -1 → "overdue", never "due
 * today"). null if unparseable. */
export function daysUntil(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((t - now.getTime()) / 86400000);
}

export type ChipTone = "rose" | "amber" | "neutral";

/** The one status chip a collapsed proposal card shows: its deadline when it
 * has one (overdue in red), else "Likely stale" when the review says so, else
 * how long it has waited. The full chip row shows when the card is opened. */
export function proposalStatusChip(
  p: { due_at?: string | null; review_verdict?: string; created_at: string },
  now: Date = new Date(),
): { label: string; tone: ChipTone } | null {
  const dueIn = daysUntil(p.due_at, now);
  if (dueIn != null) {
    if (dueIn < 0) return { label: "Overdue", tone: "rose" };
    return { label: dueIn === 0 ? "Due today" : `Due in ${dueIn}d`, tone: "amber" };
  }
  if ((p.review_verdict ?? "") === "likely_stale") return { label: "Likely stale", tone: "amber" };
  const age = ageLabel(p.created_at, now);
  return age ? { label: `${age} old`, tone: "neutral" } : null;
}
