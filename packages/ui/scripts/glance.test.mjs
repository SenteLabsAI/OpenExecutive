import assert from "node:assert/strict";
import test from "node:test";
import {
  goalTotals,
  handledThisWeek,
  realDepartments,
  scheduledWorkflows,
  watching,
} from "../src/lib/glance.ts";

const NOW = new Date("2026-10-25T12:00:00Z");
const daysAgo = (n) => new Date(NOW.getTime() - n * 24 * 60 * 60 * 1000).toISOString();

const dept = (slug, goals, atRisk = 0, offTrack = 0) => ({
  slug,
  title: slug || "Other goals",
  authority_level: "draft",
  goal_count: goals,
  at_risk_count: atRisk,
  off_track_count: offTrack,
  awaiting_count: 0,
});

test("the row of goals with no department is not a department", () => {
  const rows = [dept("", 2), dept("sales", 1)];
  assert.deepEqual(realDepartments(rows).map((d) => d.slug), ["sales"]);
  assert.deepEqual(realDepartments([dept("", 3)]), []);
});

test("goal totals count every goal, with or without a department", () => {
  assert.deepEqual(goalTotals([dept("", 2, 1), dept("sales", 3, 0, 1)]), { goals: 5, atRisk: 2 });
  assert.deepEqual(goalTotals([]), { goals: 0, atRisk: 0 });
});

test("Act as me counts what it handled in the last seven days", () => {
  const replies = [{ sent_at: daysAgo(1) }, { sent_at: daysAgo(6.9) }, { sent_at: daysAgo(8) }, { sent_at: "" }];
  assert.equal(handledThisWeek(replies, NOW), 2);
});

test("workflows count your active scheduled ones and only their runs", () => {
  const defs = [
    { name: "weekly", cadence: "weekly", is_active: true },
    { name: "paused", cadence: "daily", is_active: false },
    { name: "manual", cadence: null },
  ];
  const runs = [
    { workflow_name: "weekly", created_at: daysAgo(2) },
    { workflow_name: "weekly", created_at: daysAgo(10) },
    { workflow_name: "morning_brief", created_at: daysAgo(1) },
  ];
  assert.deepEqual(scheduledWorkflows(defs, runs, NOW), { scheduled: 1, runsThisWeek: 1 });
});

test("watching counts enabled entries and those that fired this week", () => {
  const items = [
    { enabled: true, last_fired_at: daysAgo(3) },
    { enabled: true, last_fired_at: null },
    { enabled: false, last_fired_at: daysAgo(1) },
  ];
  assert.deepEqual(watching(items, NOW), { watched: 2, firedThisWeek: 1 });
});
