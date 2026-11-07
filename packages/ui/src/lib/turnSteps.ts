// The steps a long chat turn has taken so far, from its `activity` events.
// Each round names what it is doing; a repeat of the step already showing
// (the same read run again, or two rounds of "Working…") is one step.

export const MAX_STEPS = 12;

// Shown as the long-turn line (components/TurnSteps.tsx) once the turn has done
// this many different things. A quick answer keeps the one-line status.
export const STEPS_SHOWN_FROM = 2;

export function addStep(steps: string[], label: string | null | undefined): string[] {
  const clean = (label ?? "").trim();
  if (!clean || steps[steps.length - 1] === clean) return steps;
  const next = [...steps, clean];
  // Keep the newest: the list is about where the work is now.
  return next.length > MAX_STEPS ? next.slice(next.length - MAX_STEPS) : next;
}

export function showSteps(steps: string[]): boolean {
  return steps.length >= STEPS_SHOWN_FROM;
}

// What a long turn shows while it works (`progress` events from the
// backend's `show_progress`, orchestrator/turn_progress.py): the task, what it
// is doing now, what it has used and what it has found so far. The card stays
// the same size however long the turn runs: the first few sources and the
// newest few findings show, the rest fold into a count.

export interface TurnProgress {
  task: string;
  now: string;
  used: string[];
  found: string[];
}

// Answer text this long since the last tool round means the turn is writing
// its reply, not a line before its next tools.
export const WRITING_FROM_CHARS = 280;

export const USED_SHOWN = 3;
export const FOUND_SHOWN = 3;

function cleanLines(raw: unknown): string[] {
  return Array.isArray(raw)
    ? raw.filter((s): s is string => typeof s === "string" && s.trim() !== "").map((s) => s.trim())
    : [];
}

function cleanLine(raw: unknown): string {
  return typeof raw === "string" ? raw.trim() : "";
}

// Each event carries the whole picture so far, so it replaces the last one;
// one with nothing in it keeps what was showing.
export function applyProgress(
  prev: TurnProgress | null,
  item: { task?: unknown; now?: unknown; used?: unknown; found?: unknown },
): TurnProgress | null {
  const next = {
    task: cleanLine(item.task),
    now: cleanLine(item.now),
    used: cleanLines(item.used),
    found: cleanLines(item.found),
  };
  if (!next.task && !next.now && next.used.length === 0 && next.found.length === 0) return prev;
  return next;
}

// The first few, then how many more: the first things a turn reads are
// usually what it was asked about.
export function firstFew(items: string[], n: number): { shown: string[]; more: number } {
  return { shown: items.slice(0, n), more: Math.max(0, items.length - n) };
}

// The newest few, and how many earlier ones fold away.
export function newestFew(items: string[], n: number): { shown: string[]; earlier: number } {
  return { shown: items.slice(-n), earlier: Math.max(0, items.length - n) };
}

// What the turn read, counted by kind from each round's `activity` `reads`,
// for when the turn names nothing itself ("3 emails", "2 files").
export type ReadCounts = Record<string, number>;

export function addReads(prev: ReadCounts, reads: unknown): ReadCounts {
  if (!reads || typeof reads !== "object") return prev;
  let next = prev;
  for (const [kind, n] of Object.entries(reads as Record<string, unknown>)) {
    if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) continue;
    if (next === prev) next = { ...prev };
    next[kind] = (next[kind] ?? 0) + Math.trunc(n);
  }
  return next;
}

const PLURALS: Record<string, string> = {
  person: "people",
  "web search": "web searches",
};

// Kinds read once a turn, or not worth counting, say what they are.
const UNCOUNTED: Record<string, string> = {
  calendar: "Your calendar",
  "connected app": "Connected apps",
};

export function readChips(reads: ReadCounts): string[] {
  return Object.entries(reads)
    .filter(([, n]) => n > 0)
    .map(([kind, n]) => {
      if (UNCOUNTED[kind]) return UNCOUNTED[kind];
      return n === 1 ? `1 ${kind}` : `${n} ${PLURALS[kind] ?? `${kind}s`}`;
    });
}

// The tap target under a long turn's live line: how many sources it has used
// and how many things it has found ("7 sources · 3 found"), or null while it
// has neither. Named sources count one each; unnamed reads count by number.
export function detailsSummary(
  progress: TurnProgress | null,
  reads: ReadCounts,
): string | null {
  const sources = progress && progress.used.length > 0
    ? progress.used.length
    : Object.values(reads).reduce((sum, n) => sum + (n > 0 ? n : 0), 0);
  const found = progress?.found.length ?? 0;
  const parts: string[] = [];
  if (sources > 0) parts.push(sources === 1 ? "1 source" : `${sources} sources`);
  if (found > 0) parts.push(`${found} found`);
  return parts.length > 0 ? parts.join(" · ") : null;
}
