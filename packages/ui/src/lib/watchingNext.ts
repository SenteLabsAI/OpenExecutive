import type { WatchingItem } from "./api";

// Home's Watching next: when it acts on a line next, in the words the line
// reads ("I follow up with Priya Nair tomorrow at 9 am").

const WEEKDAY = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 86_400_000;

function clock(d: Date): string {
  const h = d.getHours();
  const m = d.getMinutes();
  const hour = h % 12 === 0 ? 12 : h % 12;
  return `${hour}${m ? `:${String(m).padStart(2, "0")}` : ""} ${h < 12 ? "am" : "pm"}`;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "today at 3 pm", "tomorrow at 9 am", "on Wednesday at 9 am", "on Nov 20";
 * "soon" once it is due; "" for no time. */
export function nextLabel(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  if (d.getTime() <= now.getTime()) return "soon";
  const days = Math.round((startOfDay(d) - startOfDay(now)) / DAY_MS);
  if (days === 0) return `today at ${clock(d)}`;
  if (days === 1) return `tomorrow at ${clock(d)}`;
  if (days < 7) return `on ${WEEKDAY[d.getDay()]} at ${clock(d)}`;
  return `on ${MONTH[d.getMonth()]} ${d.getDate()}`;
}

/** The line under a Watching next title: what it does next, and when. */
export function watchingLine(item: Pick<WatchingItem, "detail" | "at">, now: Date = new Date()): string {
  const when = nextLabel(item.at, now);
  return [item.detail, when].filter(Boolean).join(" ");
}
