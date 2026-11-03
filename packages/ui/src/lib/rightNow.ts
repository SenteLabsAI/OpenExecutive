// Right now: Home's card for the Executive's live read of the day (the /today
// header, rewritten as things change). These helpers say which points are
// new since the reader last looked and label the times.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/rightNow.test.mjs).

export interface TimedPoint {
  text: string;
  since?: string | null;
  source?: {
    kind: "mail" | "alert" | "link";
    message_id?: string | null;
    thread_id?: string | null;
    decision_id?: number | null;
    alert_id?: number | null;
    url?: string | null;
    app?: string | null;
  } | null;
}

/** What a point's buttons open: an email (by its ids), a card in Needs you,
 * a chat message in its app (https links only), or nothing (the point is
 * only talked through). */
export type PointTarget =
  | { kind: "mail"; threadId: string; messageId: string; cardId: number | null }
  | { kind: "alert"; alertId: number }
  | { kind: "link"; url: string; app: string }
  | null;

export function pointTarget(point: TimedPoint): PointTarget {
  const s = point.source;
  if (!s) return null;
  if (s.kind === "mail" && s.message_id && s.thread_id) {
    return { kind: "mail", threadId: s.thread_id, messageId: s.message_id, cardId: s.decision_id ?? null };
  }
  if (s.kind === "alert" && typeof s.alert_id === "number") return { kind: "alert", alertId: s.alert_id };
  if (s.kind === "link" && s.url && s.url.startsWith("https://")) {
    return { kind: "link", url: s.url, app: s.app?.trim() || "the app" };
  }
  return null;
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

/** "Checked 2:20 PM": when the served header was last found current, so a
 * quiet one doesn't read as stuck at the hour it was written. */
export function checkedLabel(iso: string | null | undefined): string | null {
  const at = clockLabel(iso);
  return at ? `Checked ${at}` : null;
}

/** Right now's closing line for mail from people the reader doesn't deal
 * with: "Also in: 2 others — Ana Ruiz: Q4 rates and Yardi: Webinar." Null
 * when there was none. */
export function alsoInLine(labels: string[] | null | undefined, total: number | null | undefined): string | null {
  const shown = (labels ?? []).filter((l) => l.trim());
  const count = Math.max(total ?? 0, shown.length);
  if (count === 0) return null;
  const head = `Also in: ${count} ${count === 1 ? "other" : "others"}`;
  if (shown.length === 0) return `${head}.`;
  const rest = count - shown.length;
  const named = rest > 0 ? [...shown, `${rest} more`] : shown;
  const list = named.length === 1 ? named[0] : `${named.slice(0, -1).join(", ")} and ${named[named.length - 1]}`;
  return `${head} — ${list}.`;
}

/** What the Home card links to. With points, the whole picture. With none
 * (a quiet day) the card already lists the next meetings, so the link names
 * what is left: the morning brief alone, the whole picture when there is a
 * move or more meetings than the card shows, or nothing at all. */
export type CardLink = "whole" | "morning" | null;

export function cardLink(
  today: {
    narrative_points?: unknown[] | null;
    narrative_move?: string | null;
    morning_brief?: unknown | null;
    calendar_next?: unknown[] | null;
  },
  shownCalendar: number,
): CardLink {
  if ((today.narrative_points ?? []).length > 0) return "whole";
  if (today.narrative_move?.trim() || (today.calendar_next ?? []).length > shownCalendar) return "whole";
  return today.morning_brief ? "morning" : null;
}

// While Right now is being rewritten: its steps, each done, running or still
// to come, from the step the server reports (reading until it starts).
export type ProgressState = "done" | "now" | "todo";

const PROGRESS_STEPS = [
  { step: "reading", now: "Checking your inbox, calendar and Needs you", done: "Checked your inbox, calendar and Needs you" },
  { step: "writing", now: "Writing what matters now", done: "Wrote what matters now" },
  { step: "checking", now: "Checking each point against your email", done: "Checked each point against your email" },
] as const;

export function progressSteps(step: string | null | undefined): { label: string; state: ProgressState }[] {
  const at = Math.max(0, PROGRESS_STEPS.findIndex((s) => s.step === step));
  return PROGRESS_STEPS.map((s, i) => ({
    label: i < at ? s.done : s.now,
    state: i < at ? "done" : i === at ? "now" : "todo",
  }));
}

/** "2 new emails since 3:12 PM", or null when none came (or no header yet). */
export function progressNote(newMail: number | null | undefined, since: string | null | undefined): string | null {
  const when = clockLabel(since);
  if (!newMail || newMail < 1 || !when) return null;
  return `${newMail} new email${newMail === 1 ? "" : "s"} since ${when}`;
}

// Do next: the header's Move today line, on the Home card with buttons that
// start it. "Later" puts it away until the move changes or the day ends.

/** The chat that "Do it" opens: the Executive starts on the move, doing what
 * it can and showing anything to send or approve first. */
export function buildMoveSeed(move: string): string {
  return (
    `Let's do this now, from Right now:\n\n"${move.trim()}"\n\n` +
    `Start on it: do the parts you can, and show me anything that needs my ` +
    `OK (an email to send, a meeting to book) before it goes.`
  );
}

/** The email the move is about: the top point's, and only when the move
 * names a word of that point's bold subject ("Dogwood" in both). A move
 * about something else must not offer to reply to an unrelated email. */
export function moveMail(
  points: TimedPoint[],
  move: string,
): Extract<NonNullable<PointTarget>, { kind: "mail" }> | null {
  const top = points[0] ? pointTarget(points[0]) : null;
  if (!top || top.kind !== "mail") return null;
  const subject = /\*\*(.+?)\*\*/.exec(points[0].text)?.[1] ?? "";
  const words = (text: string) => new Set(text.toLowerCase().match(/[\p{L}\p{N}]{4,}/gu) ?? []);
  const inMove = words(move);
  return [...words(subject)].some((w) => inMove.has(w)) ? top : null;
}

function localDay(now: Date): string {
  return `${now.getFullYear()}-${now.getMonth() + 1}-${now.getDate()}`;
}

/** What "Later" stores: this move, today. */
export function laterMark(move: string, now: Date): string {
  return JSON.stringify({ day: localDay(now), move: move.trim() });
}

/** Whether a stored "Later" covers this move today (a new move shows again,
 * and so does every move the next day). */
export function isPutOff(stored: string | null | undefined, move: string, now: Date): boolean {
  if (!stored) return false;
  try {
    const v = JSON.parse(stored) as { day?: unknown; move?: unknown };
    return v.day === localDay(now) && v.move === move.trim();
  } catch {
    return false;
  }
}
