import type { ChatTopic, SessionSummary } from "@/lib/api";

export type GroupKey = "pinned" | "today" | "yesterday" | "prev7" | "prev30" | "older";

export interface SessionGroup {
  key: GroupKey;
  label: string;
  items: SessionSummary[];
}

const GROUP_ORDER: { key: GroupKey; label: string }[] = [
  { key: "pinned", label: "Pinned" },
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "prev7", label: "Previous 7 Days" },
  { key: "prev30", label: "Previous 30 Days" },
  { key: "older", label: "Older" },
];

const DAY_MS = 86_400_000;

/**
 * Buckets sessions into date groups by `updated_at`. Input is assumed already
 * sorted newest-first (the API returns `updated_at DESC`), so item order within
 * each bucket is preserved. Empty buckets are dropped. `now` is injectable so
 * the bucketing is deterministically testable. Unparseable timestamps fall into
 * "Older" rather than throwing. Pinned chats go first, in "Pinned", whatever
 * their date.
 */
export function groupSessionsByDate(
  sessions: SessionSummary[],
  now: Date = new Date(),
): SessionGroup[] {
  const startOfToday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const startOfYesterday = startOfToday - DAY_MS;
  const sevenDaysAgo = startOfToday - 7 * DAY_MS;
  const thirtyDaysAgo = startOfToday - 30 * DAY_MS;

  const buckets: Record<GroupKey, SessionSummary[]> = {
    pinned: [],
    today: [],
    yesterday: [],
    prev7: [],
    prev30: [],
    older: [],
  };

  for (const s of sessions) {
    const t = new Date(s.updated_at).getTime();
    let key: GroupKey;
    if (s.pinned_at) {
      key = "pinned";
    } else if (Number.isNaN(t)) {
      key = "older";
    } else if (t >= startOfToday) {
      key = "today";
    } else if (t >= startOfYesterday) {
      key = "yesterday";
    } else if (t >= sevenDaysAgo) {
      key = "prev7";
    } else if (t >= thirtyDaysAgo) {
      key = "prev30";
    } else {
      key = "older";
    }
    buckets[key].push(s);
  }

  return GROUP_ORDER.map(({ key, label }) => ({
    key,
    label,
    items: buckets[key],
  })).filter((g) => g.items.length > 0);
}

export interface TopicGroup {
  /** "pinned", "topic:<id>" or "other". */
  key: string;
  label: string;
  /** The topic, for its menu; absent on Pinned and Other. */
  topic?: ChatTopic;
  items: SessionSummary[];
}

/**
 * Groups sessions by the topic each is filed under. Pinned chats stay first,
 * in "Pinned", as in the date view. Topics follow in the order the server
 * lists them (pinned topics first, then by their newest chat). A topic with
 * one chat shown here folds into "Other" unless the owner kept it (named,
 * pinned or filed a chat under it), so a run of one-off topics doesn't bury
 * the list. Chats under no topic, or one not listed, go to "Other", last.
 */
export function groupSessionsByTopic(
  sessions: SessionSummary[],
  topics: ChatTopic[],
): TopicGroup[] {
  const pinned: SessionSummary[] = [];
  const byTopic = new Map<number, SessionSummary[]>();
  const other: SessionSummary[] = [];
  const known = new Set(topics.map((t) => t.id));
  for (const s of sessions) {
    if (s.pinned_at) pinned.push(s);
    else if (s.topic_id != null && known.has(s.topic_id)) {
      const list = byTopic.get(s.topic_id) ?? [];
      list.push(s);
      byTopic.set(s.topic_id, list);
    } else other.push(s);
  }
  const groups: TopicGroup[] = [];
  if (pinned.length) groups.push({ key: "pinned", label: "Pinned", items: pinned });
  for (const topic of topics) {
    const items = byTopic.get(topic.id);
    if (!items?.length) continue;
    if (items.length === 1 && !topic.kept && !topic.pinned_at) {
      other.push(items[0]);
      continue;
    }
    groups.push({ key: `topic:${topic.id}`, label: topic.name, topic, items });
  }
  if (other.length) {
    // Keep "Other" newest first, as the list came.
    const order = new Map(sessions.map((s, i) => [s.session_id, i]));
    other.sort((a, b) => (order.get(a.session_id) ?? 0) - (order.get(b.session_id) ?? 0));
    groups.push({ key: "other", label: "Other", items: other });
  }
  return groups;
}

export type BandKey = "active" | "week" | "twoWeeks" | "month";

/** A heading in a band: one topic, or an area over two or more related ones. */
export interface TopicEntry {
  /** "topic:<id>" or "area:<name>". */
  key: string;
  label: string;
  /** The topic, for its menu; absent on an area. */
  topic?: ChatTopic;
  /** The area's name, for its menu; absent on a topic. */
  area?: string;
  /** An area's topics, most recently active first. */
  children: TopicGroup[];
  /** Every chat under it, newest first. */
  items: SessionSummary[];
  /** When its newest chat was last active. */
  lastActive: string;
}

export interface TopicBand {
  key: BandKey;
  label: string;
  entries: TopicEntry[];
}

export interface TopicLayout {
  pinned?: TopicGroup;
  bands: TopicBand[];
  other?: TopicGroup;
}

const BANDS: { key: BandKey; label: string; under: number }[] = [
  { key: "active", label: "Active this week", under: 7 },
  { key: "week", label: "Quiet for a week", under: 14 },
  { key: "twoWeeks", label: "Quiet for 2 weeks", under: 30 },
  { key: "month", label: "Quiet for a month or more", under: Infinity },
];

const at = (iso: string) => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
};

function newest(items: SessionSummary[]): string {
  return items.reduce((best, s) => (at(s.updated_at) > at(best) ? s.updated_at : best), items[0]?.updated_at ?? "");
}

function withinArea(name: string, area: string): string {
  if (!name.toLowerCase().startsWith(area.toLowerCase())) return name;
  // Only a whole word: "Boardwalk Permit" stays as is under "Board".
  const after = name.slice(area.length);
  if (after && !/^[\s:·–—-]/.test(after)) return name;
  const rest = after.replace(/^[\s:·–—-]+/, "");
  return rest ? rest[0].toUpperCase() + rest.slice(1) : name;
}

/**
 * The Topic view, laid out to stay short as topics pile up: pinned chats,
 * then each topic (or area over related topics) in a band by how long since
 * its last chat (this week, a week, two weeks, a month or more), then Other.
 * An area heads only two or more topics that still show; a lone one stands
 * as its topic. A pinned topic stays in the active band. Within a band,
 * pinned first, then the most recently active.
 */
export function layoutTopics(
  sessions: SessionSummary[],
  topics: ChatTopic[],
  now: Date = new Date(),
): TopicLayout {
  const groups = groupSessionsByTopic(sessions, topics);
  const layout: TopicLayout = { bands: [] };
  const byArea = new Map<string, TopicGroup[]>();
  const loose: TopicGroup[] = [];
  for (const g of groups) {
    if (g.key === "pinned") layout.pinned = g;
    else if (g.key === "other") layout.other = g;
    else if (g.topic?.area) {
      const key = g.topic.area.toLowerCase();
      byArea.set(key, [...(byArea.get(key) ?? []), g]);
    } else loose.push(g);
  }
  const entries: TopicEntry[] = loose.map((g) => ({
    key: g.key,
    label: g.label,
    topic: g.topic,
    children: [],
    items: g.items,
    lastActive: newest(g.items),
  }));
  for (const list of byArea.values()) {
    if (list.length < 2) {
      for (const g of list) {
        entries.push({ key: g.key, label: g.label, topic: g.topic, children: [], items: g.items, lastActive: newest(g.items) });
      }
      continue;
    }
    const area = list[0].topic?.area ?? list[0].label;
    // Under its area a topic drops the area's name it starts with
    // ("St. Albans Offers" reads "Offers" under St. Albans).
    const children = [...list]
      .sort((a, b) => at(newest(b.items)) - at(newest(a.items)))
      .map((g) => ({ ...g, label: withinArea(g.label, area) }));
    const items = children.flatMap((g) => g.items).sort((a, b) => at(b.updated_at) - at(a.updated_at));
    entries.push({ key: `area:${area.toLowerCase()}`, label: area, area, children, items, lastActive: newest(items) });
  }
  const pinned = (e: TopicEntry) =>
    e.topic ? !!e.topic.pinned_at : e.children.some((g) => !!g.topic?.pinned_at);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const bandOf = (e: TopicEntry) => {
    if (pinned(e)) return BANDS[0];
    const t = at(e.lastActive);
    const days = t >= startOfToday ? 0 : Math.ceil((startOfToday - t) / DAY_MS);
    return BANDS.find((b) => days < b.under) ?? BANDS[BANDS.length - 1];
  };
  for (const band of BANDS) {
    const inBand = entries
      .filter((e) => bandOf(e) === band)
      .sort((a, b) => Number(pinned(b)) - Number(pinned(a)) || at(b.lastActive) - at(a.lastActive));
    if (inBand.length) layout.bands.push({ key: band.key, label: band.label, entries: inBand });
  }
  return layout;
}

/** When a topic was last active, in a word: Today, Yesterday, the weekday
 *  this week, else the date. */
export function activeLabel(iso: string, now: Date = new Date()): string {
  const t = at(iso);
  if (!t) return "";
  const then = new Date(t);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (t >= startOfToday) return "Today";
  const days = Math.ceil((startOfToday - t) / DAY_MS);
  if (days <= 1) return "Yesterday";
  if (days < 7) return then.toLocaleDateString(undefined, { weekday: "short" });
  return then.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(then.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}
