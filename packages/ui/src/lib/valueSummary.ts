// Pure helpers for the "Your month" card and screen on Pulse
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

/** "+3", "−2" or "same" against the period before; "" when both are zero. */
export function deltaLabel(count: number, previous: number): string {
  if (count === 0 && previous === 0) return "";
  const diff = count - previous;
  if (diff === 0) return "same";
  return diff > 0 ? `+${diff}` : `−${Math.abs(diff)}`;
}

/** Lines with something in this period or the one before; the rest is noise. */
export function visibleLines<T extends ValueLineLike>(lines: T[]): T[] {
  return lines.filter((l) => l.count > 0 || l.previous > 0);
}
