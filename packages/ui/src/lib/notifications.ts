// Settings → Notifications: the signed-in person's own quiet hours, digest or
// instant, and whether urgent things get through (GET/PUT
// /notifications/settings). Types and wording only, kept free of React so
// scripts/ can test it.

export type QuietDays = "every_day" | "weekdays";
export type Delivery = "instant" | "digest";

export interface QuietHours {
  enabled: boolean;
  /** HH:MM, 24-hour, in `effective_timezone`. May be later than `end`. */
  start: string;
  end: string;
  days: QuietDays;
}

export interface NotificationSettings {
  quiet_hours: QuietHours;
  delivery: Delivery;
  urgent_through: boolean;
  /** The zone they chose; "" for the workspace's. */
  timezone: string;
  effective_timezone: string;
  /** Messages waiting for quiet hours to end or for their next brief. */
  held_count: number;
  /** They get the morning and end-of-day briefs (the account owner). */
  gets_briefs: boolean;
  updated_at: string;
}

export interface NotificationSettingsUpdate {
  quiet_hours: QuietHours;
  delivery: Delivery;
  urgent_through: boolean;
  timezone: string;
}

export const DAYS_LABEL: Record<QuietDays, string> = {
  every_day: "Every day",
  weekdays: "Weekdays (weekends quiet all day)",
};

/** "10:00 pm" for "22:00". */
export function clockLabel(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return hhmm;
  const suffix = h < 12 ? "am" : "pm";
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${suffix}`;
}

/** Whether the quiet hours run past midnight (22:00 → 07:00). */
export function crossesMidnight(q: Pick<QuietHours, "start" | "end">): boolean {
  return q.end <= q.start;
}

/** One line saying what the quiet hours do, from the person's side. */
export function quietSummary(q: QuietHours): string {
  if (!q.enabled) return "Off. Messages can reach you at any hour.";
  const span = `${clockLabel(q.start)} to ${clockLabel(q.end)}${crossesMidnight(q) ? " the next morning" : ""}`;
  return q.days === "weekdays"
    ? `Quiet from ${span} on weekdays, and all weekend.`
    : `Quiet from ${span}, every day.`;
}

/** Why the form can't be saved yet, or null. */
export function quietError(q: QuietHours): string | null {
  if (!q.enabled) return null;
  if (!/^\d{2}:\d{2}$/.test(q.start) || !/^\d{2}:\d{2}$/.test(q.end)) return "Pick a start and end time.";
  if (q.start === q.end) return "Quiet hours need to start and end at different times.";
  return null;
}

/** What "In my briefs" means for this person: the owner gets briefs; anyone
 * else gets the held messages on their own when the briefs go out. */
export function digestDescription(getsBriefs: boolean): string {
  return getsBriefs
    ? "Held and added to your next morning brief or end-of-day digest, under “While you were away”."
    : "Held and sent to you together, in one message, when the morning and end-of-day briefs go out.";
}

/** "2 messages are waiting for you", or null when none are. */
export function heldLine(count: number, delivery: Delivery): string | null {
  if (count <= 0) return null;
  const what = count === 1 ? "1 message is" : `${count} messages are`;
  return delivery === "digest"
    ? `${what} waiting for your next brief.`
    : `${what} waiting for your quiet hours to end.`;
}

/** The PUT body for a form state; the browser's zone fills an unset one, so
 * the times mean what the person sees. */
export function toUpdate(
  s: Pick<NotificationSettings, "quiet_hours" | "delivery" | "urgent_through" | "timezone">,
  browserZone: string | null,
): NotificationSettingsUpdate {
  return {
    quiet_hours: { ...s.quiet_hours },
    delivery: s.delivery,
    urgent_through: s.urgent_through,
    timezone: s.timezone || browserZone || "",
  };
}

export const DELIVERY_LABEL: Record<Delivery, string> = {
  instant: "As they happen",
  digest: "In my briefs",
};

/** The Settings hub tile's one-line status. */
export function tileStatus(s: Pick<NotificationSettings, "quiet_hours" | "delivery">): string {
  const q = s.quiet_hours;
  const quiet = q.enabled ? `Quiet ${clockLabel(q.start)}–${clockLabel(q.end)}` : "Quiet hours off";
  return `${quiet} · ${DELIVERY_LABEL[s.delivery]}`;
}
