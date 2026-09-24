import assert from "node:assert/strict";
import test from "node:test";
import {
  deriveVitals,
  formatTrend,
  formatVitalDate,
} from "../src/lib/heartbeatVitals.ts";

// Build a dense oldest → newest window from a list of counts; the last entry
// is "today". Dates are consecutive UTC days ending 2026-09-24.
function window(counts) {
  const end = Date.UTC(2026, 8, 24);
  return counts.map((count, i) => ({
    date: new Date(end - (counts.length - 1 - i) * 86_400_000).toISOString().slice(0, 10),
    count,
  }));
}

const zeros = (n) => Array.from({ length: n }, () => 0);

// --- empty / idle ------------------------------------------------------------

test("all-zero window degrades to zeros and nulls", () => {
  const v = deriveVitals(window(zeros(90)));
  assert.deepEqual(v.currentStreak, { days: 0, throughToday: false });
  assert.equal(v.longestStreak, 0);
  assert.equal(v.total, 0);
  assert.equal(v.activeDays, 0);
  assert.equal(v.avgPerActiveDay, null);
  assert.equal(v.busiestDay, null);
  assert.equal(v.trend.pct, null);
  assert.equal(v.trend.direction, "none");
  assert.equal(formatTrend(v.trend), "—");
});

test("an empty list does not throw", () => {
  const v = deriveVitals([]);
  assert.deepEqual(v.currentStreak, { days: 0, throughToday: false });
  assert.equal(v.trend.windowDays, 0);
  assert.equal(v.busiestDay, null);
});

// --- streaks -----------------------------------------------------------------

test("current streak counts through today when today is active", () => {
  const v = deriveVitals(window([0, 1, 2, 3]));
  assert.deepEqual(v.currentStreak, { days: 3, throughToday: true });
});

test("current streak counts back from yesterday when today is still idle", () => {
  const v = deriveVitals(window([0, 2, 1, 4, 0]));
  assert.deepEqual(v.currentStreak, { days: 3, throughToday: false });
});

test("current streak is 0 when both today and yesterday are idle", () => {
  const v = deriveVitals(window([3, 3, 0, 0]));
  assert.equal(v.currentStreak.days, 0);
});

test("longest streak is found mid-window, distinct from the current one", () => {
  const v = deriveVitals(window([1, 0, 1, 1, 1, 1, 0, 2, 2]));
  assert.equal(v.longestStreak, 4);
  assert.deepEqual(v.currentStreak, { days: 2, throughToday: true });
});

// --- totals / averages / busiest ---------------------------------------------

test("total, active days, and average per active day", () => {
  const v = deriveVitals(window([0, 3, 0, 4, 0]));
  assert.equal(v.total, 7);
  assert.equal(v.activeDays, 2);
  assert.equal(v.avgPerActiveDay, 3.5);
});

test("average rounds to one decimal", () => {
  const v = deriveVitals(window([1, 1, 2]));
  assert.equal(v.avgPerActiveDay, 1.3);
});

test("busiest-day tie goes to the most recent day", () => {
  const days = window([5, 1, 5, 2]);
  const v = deriveVitals(days);
  assert.deepEqual(v.busiestDay, days[2]);
});

// --- trend -------------------------------------------------------------------

// Trend compares complete days only, so each case appends an in-progress
// "today" whose count must not move the result.
const withToday = (completeCounts, today = 0) => window([...completeCounts, today]);

test("trend compares the last 30 complete days to the 30 before", () => {
  // 89 complete days: first 29 ignored, prior 30 sum to 30, last 30 sum to 36.
  const counts = [...Array(29).fill(9), ...Array(30).fill(1), ...Array(24).fill(1), ...Array(6).fill(2)];
  const v = deriveVitals(withToday(counts, 50));
  assert.equal(v.trend.windowDays, 30);
  assert.equal(v.trend.prior, 30);
  assert.equal(v.trend.current, 36);
  assert.equal(v.trend.pct, 20);
  assert.equal(v.trend.direction, "up");
  assert.equal(formatTrend(v.trend), "▲ 20%");
});

test("a steady rhythm reads flat while today is still idle", () => {
  // One beat a day for 89 days, nothing yet today (early UTC morning).
  const v = deriveVitals(withToday(Array(89).fill(1), 0));
  assert.equal(v.trend.current, 30);
  assert.equal(v.trend.prior, 30);
  assert.equal(v.trend.direction, "flat");
});

test("a drop reads as down with an absolute percentage", () => {
  const v = deriveVitals(withToday([...Array(30).fill(2), ...Array(30).fill(1)]));
  assert.equal(v.trend.pct, -50);
  assert.equal(formatTrend(v.trend), "▼ 50%");
});

test("equal halves read as flat", () => {
  const v = deriveVitals(withToday(Array(60).fill(1), 7));
  assert.equal(v.trend.direction, "flat");
  assert.equal(formatTrend(v.trend), "flat");
});

test("no prior activity: pct is null and direction is new", () => {
  const v = deriveVitals(withToday([...zeros(30), ...Array(30).fill(1)]));
  assert.equal(v.trend.pct, null);
  assert.equal(v.trend.direction, "new");
  assert.equal(formatTrend(v.trend), "new");
});

test("a window shorter than 60 days compares the halves that exist", () => {
  const v = deriveVitals(withToday([1, 1, 1, 1, 2, 2, 2, 2, 5], 9));
  // 9 complete days → 4-day halves: prior = 1+1+1+2, current = 2+2+2+5.
  assert.equal(v.trend.windowDays, 4);
  assert.equal(v.trend.prior, 5);
  assert.equal(v.trend.current, 11);
});

// --- formatting --------------------------------------------------------------

test("formatVitalDate formats in UTC", () => {
  assert.equal(formatVitalDate("2026-09-08"), "Tue, Sep 8");
});
