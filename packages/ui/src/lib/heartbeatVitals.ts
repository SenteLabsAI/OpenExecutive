// Vitals for the Pulse "Heartbeat" card: summary numbers derived from the same
// dense per-day counts the heatmap draws (GET /today/activity/daily — every
// calendar day present, oldest → newest, the last entry is today, UTC).
// Kept dependency-free so `node --test` can import it directly.

/** One day of activity; structurally the same as `DailyActivityCount` in api.ts. */
export interface ActivityDay {
  date: string; // YYYY-MM-DD (UTC)
  count: number;
}

/** Days per side of the "recent vs prior" comparison when the window allows. */
export const TREND_WINDOW_DAYS = 30;

export type TrendDirection = "up" | "down" | "flat" | "new" | "none";

export interface HeartbeatTrend {
  /** Days in each half of the comparison (≤ TREND_WINDOW_DAYS). */
  windowDays: number;
  current: number;
  prior: number;
  /** Rounded % change vs prior; null when prior is 0 (no baseline). */
  pct: number | null;
  direction: TrendDirection;
}

export interface HeartbeatVitals {
  /**
   * Consecutive active days ending today — or ending yesterday when nothing has
   * fired yet today, so the streak doesn't read as broken first thing.
   */
  currentStreak: { days: number; throughToday: boolean };
  longestStreak: number;
  total: number;
  activeDays: number;
  /** Mean count over active days, 1 decimal; null when there were none. */
  avgPerActiveDay: number | null;
  /** Highest-count day (most recent wins a tie); null when every day is 0. */
  busiestDay: ActivityDay | null;
  trend: HeartbeatTrend;
}

function sum(days: ActivityDay[]): number {
  return days.reduce((n, d) => n + d.count, 0);
}

function currentStreak(days: ActivityDay[]): HeartbeatVitals["currentStreak"] {
  let end = days.length - 1;
  const throughToday = end >= 0 && days[end].count > 0;
  if (!throughToday) end -= 1; // today idle so far: count back from yesterday
  let n = 0;
  for (let i = end; i >= 0 && days[i].count > 0; i--) n++;
  return { days: n, throughToday };
}

function trend(days: ActivityDay[]): HeartbeatTrend {
  const windowDays = Math.min(TREND_WINDOW_DAYS, Math.floor(days.length / 2));
  const current = windowDays > 0 ? sum(days.slice(-windowDays)) : 0;
  const prior = windowDays > 0 ? sum(days.slice(-2 * windowDays, -windowDays)) : 0;
  if (prior === 0) {
    return { windowDays, current, prior, pct: null, direction: current > 0 ? "new" : "none" };
  }
  const pct = Math.round(((current - prior) / prior) * 100);
  const direction: TrendDirection = pct > 0 ? "up" : pct < 0 ? "down" : "flat";
  return { windowDays, current, prior, pct, direction };
}

export function deriveVitals(days: ActivityDay[]): HeartbeatVitals {
  let total = 0;
  let activeDays = 0;
  let longest = 0;
  let run = 0;
  let busiest: ActivityDay | null = null;

  for (const day of days) {
    total += day.count;
    if (day.count > 0) {
      activeDays++;
      run++;
      longest = Math.max(longest, run);
      // `>=` so a later day with the same count wins the tie.
      if (busiest === null || day.count >= busiest.count) busiest = day;
    } else {
      run = 0;
    }
  }

  return {
    currentStreak: currentStreak(days),
    longestStreak: longest,
    total,
    activeDays,
    avgPerActiveDay: activeDays > 0 ? Math.round((total / activeDays) * 10) / 10 : null,
    busiestDay: busiest,
    trend: trend(days),
  };
}

/** "Tue, Sep 9" for a YYYY-MM-DD date, formatted in UTC to match the buckets. */
export function formatVitalDate(date: string): string {
  return new Date(`${date}T00:00:00Z`).toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Display text for a trend: "▲ 18%", "▼ 12%", "flat", "new", or "—". */
export function formatTrend(t: HeartbeatTrend): string {
  switch (t.direction) {
    case "up":
      return `▲ ${t.pct}%`;
    case "down":
      return `▼ ${Math.abs(t.pct ?? 0)}%`;
    case "flat":
      return "flat";
    case "new":
      return "new";
    case "none":
      return "—";
  }
}
