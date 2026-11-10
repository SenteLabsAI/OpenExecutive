// Needs you on Home: how its cards are grouped and which fold into "Older".
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/needsYou.test.mjs).

// A card nobody has touched for this long folds into the "Older (N)" line.
export const NEEDS_YOU_OLDER_AFTER_DAYS = 3;

// What a card's age is read from: when it last changed (a re-firing
// situation is not old), else when it came.
export interface Aged {
  created_at: string;
  last_seen_at?: string | null;
}

export function ageAnchor(item: Aged): string {
  return item.last_seen_at || item.created_at;
}

// True when the card is at least `days` old at `now`. A card with no
// readable time is never old, so it stays in view.
export function isOlder(at: string, now: Date, days: number = NEEDS_YOU_OLDER_AFTER_DAYS): boolean {
  const t = Date.parse(at);
  return !Number.isNaN(t) && t <= now.getTime() - days * 86400000;
}

// Split cards into the ones shown and the ones folded under "Older",
// keeping their order. `keep` marks a card that never folds (one answered
// only on its own card).
export function splitOlder<T>(
  items: T[],
  at: (item: T) => string,
  now: Date,
  keep: (item: T) => boolean = () => false,
  days: number = NEEDS_YOU_OLDER_AFTER_DAYS,
): { current: T[]; older: T[] } {
  const current: T[] = [];
  const older: T[] = [];
  for (const item of items) {
    (isOlder(at(item), now, days) && !keep(item) ? older : current).push(item);
  }
  return { current, older };
}

// Proposals in queue order, with the ones a request held together (same
// `group_key`) gathered into one entry where the first of them stood. A
// key only one card carries stays a single card.
export function groupByRequest<T extends { group_key?: string | null }>(
  proposals: T[],
): T[][] {
  const counts = new Map<string, number>();
  for (const p of proposals) {
    if (p.group_key) counts.set(p.group_key, (counts.get(p.group_key) ?? 0) + 1);
  }
  const groups = new Map<string, T[]>();
  const out: T[][] = [];
  for (const p of proposals) {
    const key = p.group_key;
    if (!key || (counts.get(key) ?? 0) < 2) {
      out.push([p]);
      continue;
    }
    const held = groups.get(key);
    if (held) {
      held.push(p);
    } else {
      const group = [p];
      groups.set(key, group);
      out.push(group);
    }
  }
  return out;
}

// The queue with the cards someone else is waiting on first (a teammate's
// ask held for your OK), each part in its own order.
export function waitedOnFirst<T extends { waited?: boolean }>(items: T[]): T[] {
  return [...items.filter((i) => i.waited), ...items.filter((i) => !i.waited)];
}

// The newest age anchor in a group, so a group is old only when all of it is.
export function newestAnchor(items: Aged[]): string {
  let best = "";
  let bestT = -Infinity;
  for (const item of items) {
    const at = ageAnchor(item);
    const t = Date.parse(at);
    if (!Number.isNaN(t) && t > bestT) {
      best = at;
      bestT = t;
    }
  }
  return best;
}

// Clear all on the Older line: closes each card its own way, all at once,
// and says which closed and which didn't (those stay listed, so the reader
// can try again).
export async function clearAll<T extends { clear: () => Promise<void> }>(
  items: T[],
): Promise<{ cleared: T[]; failed: T[] }> {
  const results = await Promise.allSettled(items.map((item) => item.clear()));
  const cleared: T[] = [];
  const failed: T[] = [];
  results.forEach((r, i) => (r.status === "fulfilled" ? cleared : failed).push(items[i]));
  return { cleared, failed };
}

// What the Older line says when some cards didn't clear.
export function clearNotice(failed: number, total: number): string | null {
  return failed > 0 ? `${failed} of ${total} didn't clear. They're still under Older. Try again.` : null;
}

// The group card's button: "Approve 3", or "Approve" for one.
export function approveLabel(n: number): string {
  return n > 1 ? `Approve ${n}` : "Approve";
}

// A card that's a signal from outside (the watch list, an app's new entries,
// a watched stock's move) is for the reader to know: Got it, nothing to
// approve, unless it says it needs an OK. A card that asks or is for them to
// know has nothing to approve either.
export function cardButtons(
  needs: string,
  isSignal: boolean,
): { noApprove: boolean; gotIt: boolean } {
  return {
    noApprove: needs === "answer" || needs === "choice" || needs === "fyi" || (isSignal && needs !== "ok"),
    gotIt: needs === "fyi" || (isSignal && !needs),
  };
}

// Whether a card's tags or fields mark it as a signal from outside.
export function isSignalCard(p: { category?: string | null; surfaced_reason?: string | null; topic_tags?: string[] | null }): boolean {
  return (
    p.category === "monitoring" ||
    Boolean(p.surfaced_reason) ||
    (p.topic_tags ?? []).some((t) => t.startsWith("external:"))
  );
}

// The name on a "<name> asked" chip: the first name, past a title such as
// "Dr." ("Someone …" stays whole).
export function askerName(name: string): string {
  const n = name.trim();
  if (n.startsWith("Someone")) return n;
  const words = n.split(/\s+/);
  const first = words.find((w) => !/^(dr|mr|mrs|ms|mx|prof)\.?$/i.test(w));
  return first ?? words[0];
}
