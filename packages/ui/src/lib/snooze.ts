/**
 * Snooze choices for a card on Home: later today, tomorrow morning, next
 * Monday morning, in the viewer's own clock. The card leaves the queue and
 * comes back on its own at that time (POST /alerts/{id}/snooze).
 */

export interface SnoozeChoice {
  /** Menu label, e.g. "Snooze until tomorrow 9:00". */
  label: string;
  /** Short form for the "Snoozed until …" notice. */
  short: string;
  until: Date;
}

const MORNING_HOUR = 9;
const LATER_TODAY_HOURS = 3;

function clock(d: Date): string {
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function atMorning(d: Date, daysAhead: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + daysAhead);
  out.setHours(MORNING_HOUR, 0, 0, 0);
  return out;
}

export function snoozeChoices(now: Date = new Date()): SnoozeChoice[] {
  const choices: SnoozeChoice[] = [];
  // Later today: three hours on, rounded up to the quarter hour. Offered only
  // while that still lands today.
  const later = new Date(now.getTime() + LATER_TODAY_HOURS * 3600_000);
  later.setSeconds(0, 0);
  later.setMinutes(Math.ceil(later.getMinutes() / 15) * 15);
  if (later.getDate() === now.getDate()) {
    choices.push({ label: `Snooze until ${clock(later)}`, short: clock(later), until: later });
  }
  const tomorrow = atMorning(now, 1);
  choices.push({
    label: `Snooze until tomorrow ${clock(tomorrow)}`,
    short: `tomorrow ${clock(tomorrow)}`,
    until: tomorrow,
  });
  // Next Monday; a week on when today is Monday. Left out on a Sunday, where
  // it would be the same as tomorrow.
  const toMonday = (8 - now.getDay()) % 7 || 7;
  if (toMonday > 1) {
    const monday = atMorning(now, toMonday);
    choices.push({
      label: `Snooze until Monday ${clock(monday)}`,
      short: `Monday ${clock(monday)}`,
      until: monday,
    });
  }
  return choices;
}
