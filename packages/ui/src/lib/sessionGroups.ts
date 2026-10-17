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
