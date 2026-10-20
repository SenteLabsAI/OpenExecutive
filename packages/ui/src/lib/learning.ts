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

/** The finer reason within a kind (`CAUSES` in `memory/chat_review`). */
export type MissCause = "term" | "fact" | "preference" | "specialist" | "do_it" | "tool" | "cant";

/** What a miss is filed under: its cause, or its kind when the grade that
 * made it predates causes or gave none. */
export type MissTopic = MissCause | MissKind;

/** The order the topics show in: each kind's causes, then the kind itself
 * for the misses without one. */
export const MISS_TOPICS: MissTopic[] = [
  "term",
  "fact",
  "preference",
  "specialist",
  "do_it",
  "company",
  "tool",
  "cant",
  "product",
];

const KIND_CAUSES: Record<MissKind, readonly string[]> = {
  company: ["term", "fact", "preference", "specialist", "do_it"],
  product: ["tool", "cant"],
};

export function missTopic(m: { kind: MissKind; cause?: string | null }): MissTopic {
  return m.cause && KIND_CAUSES[m.kind].includes(m.cause) ? (m.cause as MissCause) : m.kind;
}

/** The topics in order, each with its misses, leaving out an empty one. */
export function groupByTopic<T extends { kind: MissKind; cause?: string | null }>(
  misses: T[],
): { topic: MissTopic; items: T[] }[] {
  return MISS_TOPICS.map((topic) => ({ topic, items: misses.filter((m) => missTopic(m) === topic) })).filter(
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

/**
 * A forget that waits for Undo. `hold` keeps the items for `delayMs`, then
 * sends each and tells `onSent` how many failed; a second `hold` sends the
 * first batch straight away; `undo` takes the batch back unsent; `flush`
 * sends it now (unmount, page close).
 */
export class DeferredForget<T> {
  private batch: {
    items: T[];
    onSent: (items: T[], failed: number) => void;
    timer: ReturnType<typeof setTimeout>;
  } | null = null;
  private readonly send: (item: T, keepalive: boolean) => Promise<unknown>;
  private readonly delayMs: number;
  constructor(send: (item: T, keepalive: boolean) => Promise<unknown>, delayMs: number) {
    this.send = send;
    this.delayMs = delayMs;
  }

  get waiting(): T[] {
    return this.batch ? [...this.batch.items] : [];
  }

  hold(items: T[], onSent: (items: T[], failed: number) => void = () => {}): void {
    void this.flush();
    if (items.length === 0) return;
    this.batch = { items, onSent, timer: setTimeout(() => void this.flush(), this.delayMs) };
  }

  /** The batch taken back, or [] when nothing was waiting. */
  undo(): T[] {
    const b = this.take();
    return b ? b.items : [];
  }

  async flush({ keepalive = false }: { keepalive?: boolean } = {}): Promise<void> {
    const b = this.take();
    if (!b) return;
    const results = await Promise.allSettled(b.items.map((item) => this.send(item, keepalive)));
    b.onSent(b.items, results.filter((r) => r.status === "rejected").length);
  }

  private take() {
    const b = this.batch;
    if (b) clearTimeout(b.timer);
    this.batch = null;
    return b;
  }
}
