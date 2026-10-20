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
