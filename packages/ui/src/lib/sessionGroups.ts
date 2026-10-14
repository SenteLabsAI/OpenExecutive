import type { SessionSummary } from "@/lib/api";

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
