// Pure helpers for the month count on Pulse's Done for you panel and its screen
// (components/memories/ValueSection.tsx). Kept free of React so
// scripts/valueSummary.test.mjs can run them under plain node.

export interface ValueLineLike {
  key: string;
  label: string;
  count: number;
  previous: number;
}

// The lines the overview card's one sentence names, in order, and how each
// reads with its number in front.
const HEADLINE: { key: string; one: string; many: string }[] = [
  { key: "messages_sent", one: "message sent", many: "messages sent" },
  { key: "inbox_sent", one: "inbox reply sent", many: "inbox replies sent" },
  { key: "meetings_booked", one: "meeting booked", many: "meetings booked" },
  { key: "reports", one: "report", many: "reports" },
  { key: "follow_ups", one: "follow-up", many: "follow-ups" },
  { key: "tasks_closed", one: "task closed", many: "tasks closed" },
  { key: "answered", one: "request answered", many: "requests answered" },
];

/** "42 messages sent, 6 reports, 3 meetings booked": up to `max` non-zero
 * headline lines, or "" when there are none. */
export function headline(lines: ValueLineLike[], max = 3): string {
  const byKey = new Map(lines.map((l) => [l.key, l]));
  const parts: string[] = [];
  for (const h of HEADLINE) {
    const line = byKey.get(h.key);
    if (!line || line.count <= 0) continue;
    parts.push(`${line.count} ${line.count === 1 ? h.one : h.many}`);
    if (parts.length >= max) break;
  }
  return parts.join(", ");
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** How the screen names the period shown and the one before it, from the
 * summary's label ("October 2026", "Week of 12 October"): `current` and
 * `previous` head the columns, `previousShort` goes before last period's
 * count on a phone row ("Sep"), and `heading` follows "What it did in". */
export function periodNames(
  period: "month" | "week",
  label: string,
): { current: string; previous: string; previousShort: string; heading: string } {
  if (period === "month") {
    const i = MONTHS.findIndex((m) => label.startsWith(m));
    if (i >= 0) {
      const prev = MONTHS[(i + 11) % 12];
      return { current: MONTHS[i], previous: prev, previousShort: prev.slice(0, 3), heading: MONTHS[i] };
    }
    return { current: "This month", previous: "Month before", previousShort: "Month before", heading: label };
  }
  return {
    current: label || "This week",
    previous: "Week before",
    previousShort: "Week before",
    heading: label ? `the ${label.charAt(0).toLowerCase()}${label.slice(1)}` : "this week",
  };
}

/** Lines with something in this period or the one before; the rest is noise. */
export function visibleLines<T extends ValueLineLike>(lines: T[]): T[] {
  return lines.filter((l) => l.count > 0 || l.previous > 0);
}
