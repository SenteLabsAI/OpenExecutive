// Right now: Home's card for the Executive's live read of the day (the /today
// header, rewritten as things change). These helpers say which points are
// new since the reader last looked and label the times.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/rightNow.test.mjs).

export interface TimedPoint {
  text: string;
  since?: string | null;
}

/** "2:31 PM" in the browser's zone, or null for a missing/bad time. */
export function clockLabel(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return null;
  const hours = t.getHours();
  const h12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${h12}:${String(t.getMinutes()).padStart(2, "0")} ${hours < 12 ? "AM" : "PM"}`;
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** The last look to count "new" from: only one earlier today. Before today
 * every point would be new, which says nothing. */
export function seenBaseline(seenAt: string | null | undefined, now: Date): Date | null {
  if (!seenAt) return null;
  const seen = new Date(seenAt);
  if (Number.isNaN(seen.getTime()) || !sameLocalDay(seen, now)) return null;
  return seen;
}

/** Whether a point appeared after the baseline look. */
export function isNewSince(point: TimedPoint, baseline: Date | null): boolean {
  if (!baseline || !point.since) return false;
  const since = new Date(point.since);
  return !Number.isNaN(since.getTime()) && since.getTime() > baseline.getTime();
}

/** "2 new since 11:05 AM", or null when nothing is new. */
export function newSinceLabel(points: TimedPoint[], baseline: Date | null): string | null {
  const n = points.filter((p) => isNewSince(p, baseline)).length;
  if (n === 0 || !baseline) return null;
  return `${n} new since ${clockLabel(baseline.toISOString())}`;
}
