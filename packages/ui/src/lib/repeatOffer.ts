// The words of an offer to do a repeated ask on a schedule
// (workflows/repeat_asks.py), on Workflows and under a chat reply. Pure (type
// imports only), so the node test runner can load it.
import type { RepeatOffer } from "./api";

const DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
const COUNTS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
const ASK_MAX = 90;

function weekday(d: Date, timeZone?: string): string {
  return d.toLocaleDateString("en-US", { weekday: "long", timeZone });
}

function hourWords(d: Date, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone,
  }).formatToParts(d);
  const hour = parts.find((p) => p.type === "hour")?.value ?? "";
  const minute = parts.find((p) => p.type === "minute")?.value ?? "00";
  const period = parts.find((p) => p.type === "dayPeriod")?.value ?? "";
  return `${hour}${minute === "00" ? "" : `:${minute}`} ${period}`.trim();
}

/** The next time `cadence` (UTC) fires after `now`, or null if unreadable. */
function nextRun(cadence: string, now: Date): Date | null {
  const parts = cadence.split("@");
  const time = /^(\d{1,2}):(\d{2})$/.exec(parts[parts.length - 1] ?? "");
  if (!time) return null;
  const at = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), Number(time[1]), Number(time[2])));
  if (parts[0] === "daily" && parts.length === 2) return at;
  if (parts[0] === "weekly" && parts.length === 3) {
    const dow = DOW.indexOf(parts[1].toLowerCase());
    if (dow < 0) return null;
    at.setUTCDate(at.getUTCDate() + ((dow - at.getUTCDay() + 7) % 7));
    return at;
  }
  return null;
}

/** "every Monday at 8 AM" / "every day at 9 AM", in the viewer's time. */
export function offerTiming(cadence: string, timeZone?: string, now: Date = new Date()): string {
  const at = nextRun(cadence, now);
  if (!at) return "on a schedule";
  const when = hourWords(at, timeZone);
  return cadence.startsWith("weekly@")
    ? `every ${weekday(at, timeZone)} at ${when}`
    : `every day at ${when}`;
}

function count(n: number): string {
  return COUNTS[n] ?? String(n);
}

/** Their ask, short, without its closing punctuation. */
export function offerAsk(offer: RepeatOffer): string {
  const ask = offer.ask.trim().replace(/[\s?.!]+$/, "");
  return ask.length > ASK_MAX ? `${ask.slice(0, ASK_MAX - 1).trimEnd()}…` : ask;
}

/**
 * "You have asked me for “pipeline summary” three Mondays in a row." The
 * same weekday a week apart each time is "in a row"; otherwise the days are
 * counted.
 */
export function offerHeadline(offer: RepeatOffer, timeZone?: string): string {
  const asked = offer.asked_at.map((iso) => new Date(iso)).filter((d) => !Number.isNaN(d.getTime()));
  const ask = `“${offerAsk(offer)}”`;
  const days = asked.map((d) => weekday(d, timeZone));
  const sameDay = days.length > 1 && days.every((d) => d === days[0]);
  if (sameDay) {
    const weekly = asked.every(
      (d, i) => i === 0 || Math.round((d.getTime() - asked[i - 1].getTime()) / 86_400_000 / 7) === 1,
    );
    return weekly
      ? `You have asked me for ${ask} ${count(asked.length)} ${days[0]}s in a row.`
      : `You have asked me for ${ask} on ${count(asked.length)} ${days[0]}s.`;
  }
  return `You have asked me for ${ask} on ${count(asked.length)} different days.`;
}

/** "Want me to do it every Monday at 8 AM and send it to you? …" */
export function offerQuestion(offer: RepeatOffer, timeZone?: string, now?: Date): string {
  return `Want me to do it ${offerTiming(offer.cadence, timeZone, now)} and send it to you? I will show you the steps first.`;
}

/** What Set it up hands the build-by-talking flow. */
export function offerDescription(offer: RepeatOffer, timeZone?: string, now?: Date): string {
  return `${offer.ask.trim()}\n\nDo this ${offerTiming(offer.cadence, timeZone, now)} and send it to me.`;
}
