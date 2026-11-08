// The Learning screen's numbers and lists, from what GET /memories/reflection
// already returns (memory/lessons, memory/replay). Pure, so
// scripts/learning.test.mjs runs it.

export interface LearningStats {
  /** Lessons still in use. */
  lessons: number;
  /** How many times a live lesson was used in a chat (a chat two lessons
   * reached counts twice, so it isn't called a count of chats). */
  uses: number;
  /** Misses the nightly recheck says it now gets right. */
  nowRight: number;
}

/** The numbers on the Learning screen's card. Only what went right is
 * counted: what fell short feeds the lessons and the nightly recheck, and
 * isn't shown. */
export function learningStats(
  lessons: { retired_at: string | null; shown: number }[],
  misses: { replay_state: string }[],
): LearningStats {
  const live = lessons.filter((l) => l.retired_at === null);
  return {
    lessons: live.length,
    uses: live.reduce((n, l) => n + l.shown, 0),
    nowRight: nowRight(misses).length,
  };
}

/** The misses whose last nightly replay went right: the "Now gets right" list. */
export function nowRight<T extends { replay_state: string }>(misses: T[]): T[] {
  return misses.filter((m) => m.replay_state === "closed");
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
