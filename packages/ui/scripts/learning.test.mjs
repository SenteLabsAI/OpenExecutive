import assert from "node:assert/strict";
import test from "node:test";
import { isoWeek, learningStats } from "../src/lib/learning.ts";

test("isoWeek matches Python's isocalendar, across a year end", () => {
  assert.equal(isoWeek(new Date("2026-10-19T09:00:00Z")), "2026-W43");
  assert.equal(isoWeek(new Date("2026-10-18T23:00:00Z")), "2026-W42");
  assert.equal(isoWeek(new Date("2027-01-01T12:00:00Z")), "2026-W53");
  assert.equal(isoWeek(new Date("2024-12-30T12:00:00Z")), "2025-W01");
});

test("the card counts this week only, and lessons and replays as given", () => {
  const weeks = [
    { week: "2026-W43", graded: 14, misses: 2 },
    { week: "2026-W42", graded: 30, misses: 5 },
  ];
  const now = new Date("2026-10-19T09:00:00Z");
  assert.deepEqual(learningStats(weeks, 1, { misses: 2, would_pass: 1 }, now), {
    checked: 14,
    fellShort: 2,
    lessons: 1,
    wouldGoRight: { pass: 1, of: 2 },
  });
});

test("a quiet week and no misses read as zero and nothing to check", () => {
  const now = new Date("2026-11-02T09:00:00Z");
  const stats = learningStats([{ week: "2026-W43", graded: 14, misses: 2 }], 0, { misses: 0, would_pass: 0 }, now);
  assert.equal(stats.checked, 0);
  assert.equal(stats.fellShort, 0);
  assert.equal(stats.wouldGoRight, null);
});
