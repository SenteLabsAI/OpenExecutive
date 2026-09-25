// The Briefing's "Due soon" card (solo mode): the open loops the principal
// owns — what they promised by a date, and what others asked of them — that
// are overdue or due within a week. The loops come from the existing
// GET /people/{id}/open-loops for the principal.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/dueSoon.test.mjs).

import type { OpenLoop, PersonBriefItem } from "@/lib/api";

/** How far ahead the card looks. Matches the brief's DUE THIS WEEK block. */
export const DUE_SOON_DAYS = 7;

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 24 * 60 * 60 * 1000;

export interface DueSoonItem {
  loop: OpenLoop;
  /** The loop's text, addressed to the reader (see `loopText`). */
  text: string;
  overdue: boolean;
  /** "Overdue since Sep 23" · "Due today" · "Due tomorrow" · "Due Mon, Sep 28" */
  dueLabel: string;
}

export interface DueSoon {
  /** Overdue first, then soonest due — the order the API returns. */
  items: DueSoonItem[];
  /** Open loops due after the window (not listed). */
  later: number;
}

/** The principal's person id: the oldest (lowest id) principal on the
 * roster — the same rule the backend uses — or null when there is none. */
export function principalIdOf(
  people: ReadonlyArray<Pick<PersonBriefItem, "id" | "is_principal">>,
): number | null {
  let best: number | null = null;
  for (const p of people) {
    if (p.is_principal && p.id > 0 && (best === null || p.id < best)) best = p.id;
  }
  return best;
}

/** A loop's stored description, rewritten for its owner reading it:
 * "Pat Lee committed to: send the proposal" → "Send the proposal", and
 * "Sam asked Pat Lee for: the signed contract" → "Sam asked you for: the
 * signed contract". Anything else is returned as stored. */
export function loopText(loop: Pick<OpenLoop, "description" | "owner_name">): string {
  const { description, owner_name: owner } = loop;
  const committed = `${owner} committed to: `;
  if (owner && description.startsWith(committed)) {
    const rest = description.slice(committed.length).trim();
    return rest ? rest.charAt(0).toUpperCase() + rest.slice(1) : description;
  }
  const asked = ` asked ${owner} for: `;
  const at = owner ? description.indexOf(asked) : -1;
  if (at > 0) {
    return `${description.slice(0, at)} asked you for: ${description.slice(at + asked.length)}`;
  }
  return description;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function shortDate(d: Date): string {
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

/** The label for a due time, in the viewer's local calendar. */
export function dueLabel(due: Date, now: Date): string {
  if (due.getTime() <= now.getTime()) return `Overdue since ${shortDate(due)}`;
  const days = Math.round((startOfDay(due) - startOfDay(now)) / DAY_MS);
  if (days <= 0) return "Due today";
  if (days === 1) return "Due tomorrow";
  return `Due ${WEEKDAYS[due.getDay()]}, ${shortDate(due)}`;
}

/** The loops overdue or due within `days` of `now`, labelled; the rest are
 * only counted. A loop with an unreadable due time is left out. */
export function dueSoon(
  loops: ReadonlyArray<OpenLoop>,
  now: Date = new Date(),
  days: number = DUE_SOON_DAYS,
): DueSoon {
  const horizon = now.getTime() + days * DAY_MS;
  const items: DueSoonItem[] = [];
  let later = 0;
  for (const loop of loops) {
    const due = new Date(loop.due_at);
    if (Number.isNaN(due.getTime())) continue;
    if (due.getTime() > horizon) {
      later += 1;
      continue;
    }
    items.push({
      loop,
      text: loopText(loop),
      overdue: due.getTime() <= now.getTime(),
      dueLabel: dueLabel(due, now),
    });
  }
  items.sort(
    (a, b) => new Date(a.loop.due_at).getTime() - new Date(b.loop.due_at).getTime(),
  );
  return { items, later };
}
