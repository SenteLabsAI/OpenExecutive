// The numbers on the Learning screen's "How it learns" card, from what
// GET /memories/reflection already returns (memory/chat_review weekly counts,
// memory/lessons, memory/replay). Pure, so scripts/learning.test.mjs runs it.

export interface LearningWeek {
  week: string;
  graded: number;
  misses: number;
}

export interface LearningStats {
  /** Answers the review checked this ISO week. */
  checked: number;
  /** Of those, the ones that fell short. */
  fellShort: number;
  /** Lessons still in use. */
  lessons: number;
  /** "M of N" misses from the last 30 days that would go right today, or null with none. */
  wouldGoRight: { pass: number; of: number } | null;
}

/** "2026-W42" for ``when``, as Python's ``date.isocalendar()`` writes it. */
export function isoWeek(when: Date): string {
  const d = new Date(Date.UTC(when.getUTCFullYear(), when.getUTCMonth(), when.getUTCDate()));
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const yearStart = Date.UTC(d.getUTCFullYear(), 0, 1);
  const week = Math.ceil(((d.getTime() - yearStart) / 86_400_000 + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

export function learningStats(
  weeks: LearningWeek[],
  liveLessons: number,
  replay: { misses: number; would_pass: number },
  now: Date = new Date(),
): LearningStats {
  const thisWeek = weeks.find((w) => w.week === isoWeek(now));
  return {
    checked: thisWeek?.graded ?? 0,
    fellShort: thisWeek?.misses ?? 0,
    lessons: liveLessons,
    wouldGoRight: replay.misses > 0 ? { pass: replay.would_pass, of: replay.misses } : null,
  };
}

// --- The Learning list: categories, quick picks ------------------------------

/** What `memory/chat_review` says would have fixed a miss. */
export type MissKind = "company" | "product";

export const MISS_KINDS: MissKind[] = ["company", "product"];

/** The two kinds in order, each with its misses, leaving out an empty one. */
export function groupByKind<T extends { kind: MissKind }>(misses: T[]): { kind: MissKind; items: T[] }[] {
  return MISS_KINDS.map((kind) => ({ kind, items: misses.filter((m) => m.kind === kind) })).filter(
    (g) => g.items.length > 0,
  );
}

/** The misses whose last nightly replay went right: safe to clear. */
export function fixedMissIds(misses: { id: number; replay_state: string }[]): number[] {
  return misses.filter((m) => m.replay_state === "closed").map((m) => m.id);
}

/** Live lessons whose chats so far went worse more often than fine. The
 * review retires one on its own once it has reached enough chats; these are
 * the ones heading that way. */
export function unhelpfulLessonIds(
  lessons: { id: number; retired_at: string | null; fine_after: number; miss_after: number }[],
): number[] {
  return lessons.filter((l) => l.retired_at === null && l.miss_after > l.fine_after).map((l) => l.id);
}

/** Every id in `ids` selected, or, when they all already are, none of them. */
export function toggleAll(selected: ReadonlySet<number>, ids: number[]): Set<number> {
  const next = new Set(selected);
  const all = ids.length > 0 && ids.every((id) => next.has(id));
  for (const id of ids) {
    if (all) next.delete(id);
    else next.add(id);
  }
  return next;
}
