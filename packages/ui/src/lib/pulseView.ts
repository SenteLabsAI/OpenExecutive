// Pure helpers for the Pulse page: which screen a `?tab=` link opens, and the
// short "when" labels on its Coming up and Done recently lists. Kept free of
// React so scripts/pulseView.test.mjs can run them under plain node.

/** The Pulse overview, or one of the focused screens it links to. */
export type PulseView =
  | "overview"
  | "schedule"
  | "activity"
  | "decisions"
  | "initiatives"
  | "advice"
  | "corrections"
  | "history";

export const MEMORY_VIEWS = ["decisions", "initiatives", "advice", "corrections", "history"] as const;
export type MemoryView = (typeof MEMORY_VIEWS)[number];

/**
 * The screen a `?tab=` value opens. The page used to have tabs, so older
 * links (the chat chip's `?tab=corrections`, Settings' `?tab=history`, the
 * old Heartbeat views) still land on the matching screen; anything else is
 * the overview.
 */
export function pulseViewFor(tab: string | null | undefined): PulseView {
  switch (tab) {
    case "schedule":
    case "rhythm":
    case "followups":
      return "schedule";
    case "activity":
      return "activity";
    case "decisions":
    case "initiatives":
    case "advice":
    case "corrections":
    case "history":
      return tab;
    default:
      return "overview";
  }
}

export function isMemoryView(view: PulseView): view is MemoryView {
  return (MEMORY_VIEWS as readonly string[]).includes(view);
}

const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 86_400_000;

/** "9 am", "6:30 pm" in local time; a no-break space keeps "am" with its time. */
export function clockLabel(d: Date): string {
  const h = d.getHours();
  const m = d.getMinutes();
  const hour = h % 12 === 0 ? 12 : h % 12;
  return `${hour}${m ? `:${String(m).padStart(2, "0")}` : ""}\u00a0${h < 12 ? "am" : "pm"}`;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/**
 * A short label for when something happened or will happen, in local time:
 * the clock time today ("6 pm"), "Yesterday", a weekday and time within the
 * coming week ("Fri 9 am"), a weekday within the past week ("Tue"), else the
 * date ("Oct 3"). Empty for an unparseable time.
 */
export function whenLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.round((startOfDay(d) - startOfDay(now)) / DAY_MS);
  if (days === 0) return clockLabel(d);
  if (days === -1) return "Yesterday";
  if (days > 0 && days < 7) return `${WEEKDAY[d.getDay()]} ${clockLabel(d)}`;
  if (days < 0 && days > -7) return WEEKDAY[d.getDay()];
  return `${MONTH[d.getMonth()]} ${d.getDate()}`;
}

/** whenLabel for something still to come: one already due reads "Now". */
export function upcomingLabel(iso: string, now: Date = new Date()): string {
  const at = new Date(iso).getTime();
  if (!Number.isNaN(at) && at <= now.getTime()) return "Now";
  return whenLabel(iso, now);
}
