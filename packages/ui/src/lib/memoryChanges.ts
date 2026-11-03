// Wording and links for what it remembered lately (lib/api.ts MemoryChange),
// and when the signed-in person last opened Memory in this browser.

import type { MemoryChange } from "./api";

type Kind = MemoryChange["kind"];

const NOUN: Record<Kind, [string, string]> = {
  fact: ["fact", "facts"],
  decision: ["decision", "decisions"],
  worked_out: ["figure", "figures"],
  lesson: ["lesson", "lessons"],
  note: ["note", "notes"],
};

const LABEL: Record<Kind, string> = {
  fact: "Fact",
  decision: "Decision",
  worked_out: "What it worked out",
  lesson: "Lesson",
  note: "Note",
};

const TAB: Record<Kind, string> = {
  fact: "corrections",
  decision: "corrections",
  worked_out: "worked_out",
  lesson: "reflection",
  note: "history",
};

/** "1 lesson", "2 facts · 1 figure": the counts on the chat chip. */
export function changeCounts(changes: MemoryChange[]): string {
  const order: Kind[] = ["fact", "decision", "worked_out", "lesson", "note"];
  return order
    .map((kind) => {
      const n = changes.filter((c) => c.kind === kind).length;
      return n === 0 ? "" : `${n} ${NOUN[kind][n === 1 ? 0 : 1]}`;
    })
    .filter(Boolean)
    .join(" · ");
}

/** Where the full list of that kind is on the Memory page. */
export function changeHref(change: MemoryChange): string {
  return `/memories?tab=${TAB[change.kind]}`;
}

/** "Today", "Yesterday", or "Oct 9", in local time. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(now) - day(when)) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return when.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** "Lesson · today", "What it worked out · from the DCA statement · yesterday",
 * "Fact · waiting for your OK · today". */
export function changeDetail(change: MemoryChange, now: Date = new Date()): string {
  const parts = [LABEL[change.kind]];
  if (change.kind === "worked_out" && change.source) parts.push(`from ${change.source}`);
  if (change.status === "proposed") parts.push("waiting for your OK");
  const day = dayLabel(change.created_at, now);
  if (day) parts.push(day);
  return parts.join(" · ");
}

export function changeKey(change: MemoryChange): string {
  return `${change.kind}:${change.id}`;
}

// When Memory was last opened, per browser. A missing or unreadable value
// means none yet: the first visit starts the clock rather than calling
// everything new.
const LAST_VISIT_KEY = "oe.memory.lastVisit";

export function readLastVisit(): string | null {
  try {
    return window.localStorage.getItem(LAST_VISIT_KEY);
  } catch {
    return null;
  }
}

export function writeLastVisit(iso: string): void {
  try {
    window.localStorage.setItem(LAST_VISIT_KEY, iso);
    window.dispatchEvent(new Event(MEMORY_VISITED_EVENT));
  } catch {
    // A private window: the dot just shows again next time.
  }
}

/** Sent when Memory is opened, so the sidebar's dot clears at once. */
export const MEMORY_VISITED_EVENT = "oe-memory-visited";
