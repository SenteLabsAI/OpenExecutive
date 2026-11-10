// Memory → What I learned: how the page words where a lesson was used, from
// the counts GET /memories/reflection gives per surface. Kept free of React
// so scripts/ can test it.

export const LESSON_SURFACES = ["chat", "one_on_one", "email", "meeting", "brief", "lead"] as const;
export type LessonSurfaceId = (typeof LESSON_SURFACES)[number];
export type LessonUse = Partial<Record<LessonSurfaceId, number>>;

function where(surface: LessonSurfaceId, n: number): string {
  switch (surface) {
    case "chat":
      return "in chat";
    case "one_on_one":
      return n === 1 ? "in a 1:1" : "in 1:1s";
    case "email":
      return "in email";
    case "meeting":
      return n === 1 ? "in a meeting" : "in meetings";
    case "brief":
      return n === 1 ? "in a brief" : "in briefs";
    case "lead":
      return "in work I started";
  }
}

export function totalUses(use: LessonUse | undefined): number {
  return LESSON_SURFACES.reduce((sum, s) => sum + (use?.[s] ?? 0), 0);
}

/** "not used yet", "used once, in email", "used 4 times, all in email" or
 * "used 6 times: 3 in chat, 2 in email, 1 in a 1:1" (most used first). */
export function lessonUseLine(use: LessonUse | undefined): string {
  const parts = LESSON_SURFACES.map((s) => ({ s, n: use?.[s] ?? 0 }))
    .filter((p) => p.n > 0)
    .sort((a, b) => b.n - a.n);
  const total = parts.reduce((sum, p) => sum + p.n, 0);
  if (total === 0) return "not used yet";
  if (parts.length === 1) {
    const only = where(parts[0].s, total);
    return total === 1 ? `used once, ${only}` : `used ${total} times, all ${only}`;
  }
  return `used ${total} times: ${parts.map((p) => `${p.n} ${where(p.s, p.n)}`).join(", ")}`;
}

/** The top line's "in chat, in your 1:1s and in email": the surfaces used
 * this week, most used first. "" when nothing was used. */
export function weekUseLine(use: LessonUse | undefined): string {
  const names: Record<LessonSurfaceId, string> = {
    chat: "in chat",
    one_on_one: "in your 1:1s",
    email: "in email",
    meeting: "in meetings",
    brief: "in briefs",
    lead: "in work I started",
  };
  const parts = LESSON_SURFACES.map((s) => ({ s, n: use?.[s] ?? 0 }))
    .filter((p) => p.n > 0)
    .sort((a, b) => b.n - a.n)
    .map((p) => names[p.s]);
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}
