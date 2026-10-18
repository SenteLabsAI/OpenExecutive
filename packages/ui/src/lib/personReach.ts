// The plain lines on a person's Overview: how the Executive reaches them and
// whether they are away. The ids behind them are edited under Advanced.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/personReach.test.mjs).

import type { Person } from "@/lib/api";

type ReachFields = Pick<
  Person,
  "email" | "slack_user_id" | "discord_user_id" | "telegram_chat_id" | "preferred_channel" | "response_sla_hours"
> &
  Partial<Pick<Person, "last_channel">>;

const CHANNEL_NAMES: Record<string, string> = {
  email: "email",
  slack: "Slack",
  discord: "Discord",
  telegram: "Telegram",
};

function channelPhrase(channel: string): string {
  return channel === "email" ? "by email" : `on ${CHANNEL_NAMES[channel] ?? channel}`;
}

function joinOr(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;
}

/** The channels this person has an address or id for, in a fixed order. */
export function availableChannels(p: ReachFields): string[] {
  return [
    p.email ? "email" : null,
    p.slack_user_id ? "slack" : null,
    p.discord_user_id ? "discord" : null,
    p.telegram_chat_id ? "telegram" : null,
  ].filter((c): c is string => c !== null);
}

/**
 * The order the Executive tries them in, as the server does
 * (people/reach.py): their pick, else the chat app they last talked in, then
 * Slack, Discord, Telegram, then email.
 */
export function reachOrder(p: ReachFields): string[] {
  const have = availableChannels(p);
  const chat = ["slack", "discord", "telegram"].filter((c) => have.includes(c));
  const pref = p.preferred_channel || "any";
  const first = pref !== "any" ? pref : (p.last_channel ?? null);
  const order = [...(first && chat.includes(first) ? [first] : []), ...chat.filter((c) => c !== first)];
  if (have.includes("email")) {
    if (first === "email") order.unshift("email");
    else order.push("email");
  }
  return order;
}

/**
 * "Reaches them on Discord, replies within 12h". `self` says "you" on your
 * own card; a contact has no reply time.
 */
export function reachLine(p: ReachFields, opts: { self?: boolean; contact?: boolean } = {}): string {
  const who = opts.self ? "you" : "them";
  const order = reachOrder(p);
  if (order.length === 0) return opts.self ? "No email or chat app set for you yet" : "No email or chat app set for them yet";
  const pref = p.preferred_channel || "any";
  let how: string;
  if (pref !== "any" && order[0] === pref) {
    how = `Reaches ${who} ${channelPhrase(pref)}`;
  } else if (pref === "any" && p.last_channel && order[0] === p.last_channel) {
    how = `Reaches ${who} ${channelPhrase(p.last_channel)}, where ${opts.self ? "you" : "they"} last talked`;
  } else {
    // "on Slack or Telegram", "on Telegram or by email"
    const [first, ...rest] = order;
    how = `Reaches ${who} ${joinOr([channelPhrase(first), ...rest.map((c) => (c === "email" ? "by email" : CHANNEL_NAMES[c] ?? c))])}`;
  }
  return opts.contact ? how : `${how}, replies within ${p.response_sla_hours}h`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "On leave until 24 Oct" for a YYYY-MM-DD date, with the year when it isn't this year. */
export function onLeaveLine(date: string | null | undefined, today: Date = new Date()): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(date ?? "");
  if (!m) return null;
  const [year, month, day] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const ymd = `${m[1]}-${m[2]}-${m[3]}`;
  const todayYmd = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  // A date that has passed means they are back.
  if (ymd < todayYmd) return null;
  const yearPart = year === today.getFullYear() ? "" : ` ${year}`;
  return `On leave until ${day} ${MONTHS[month - 1]}${yearPart}`;
}
