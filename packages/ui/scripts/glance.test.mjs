import assert from "node:assert/strict";
import test from "node:test";
import {
  counted,
  departmentsText,
  goalTotals,
  goalsText,
  handledThisWeek,
  peopleText,
  realDepartments,
  scheduledNames,
  scheduledWorkflows,
  tasksText,
  watching,
  watchingText,
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
  assert.equal(goalTotals([{ ...dept("", 2), unchecked_count: 2 }, dept("sales", 1)]).unchecked, 2);
  assert.deepEqual(goalTotals([dept("", 2, 1), dept("sales", 3, 0, 1)]), { goals: 5, atRisk: 2, unchecked: 0 });
  assert.deepEqual(goalTotals([]), { goals: 0, atRisk: 0, unchecked: 0 });
});

test("As you counts what it handled in the last seven days", () => {
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
  assert.deepEqual(scheduledNames(defs), ["weekly"]);
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

test("each item on Home's bottom line reads on its own", () => {
  assert.equal(counted(1, "goal", "goals"), "1 goal");
  assert.equal(counted(2, "goal", "goals"), "2 goals");
  assert.equal(tasksText(5, 0, 3), "5 tasks due this week");
  assert.equal(tasksText(1, 1, 0), "1 task due this week, 1 overdue");
  assert.equal(tasksText(0, 0, 2), "2 tasks due later");
  assert.equal(goalsText(5, 0), "5 goals on track");
  assert.equal(goalsText(5, 1), "5 goals, 1 at risk");
  assert.equal(goalsText(5, 0, 5), "5 goals, 5 not checked yet");
  assert.equal(goalsText(5, 1, 2), "5 goals, 1 at risk");
  assert.equal(departmentsText(1, 0, false), "1 department on track");
  assert.equal(departmentsText(3, 0, false, 1), "3 departments, 1 not checked yet");
  assert.equal(departmentsText(3, 2, true), "2 departments at risk");
  assert.equal(departmentsText(3, 1, false), "1 department needs attention");
  assert.equal(departmentsText(3, 2, false), "2 departments need attention");
  assert.equal(peopleText(4, 1), "1 person to follow up");
  assert.equal(peopleText(4, 0), "4 people, all clear");
  assert.equal(watchingText(6, 0), "Watching 6 things");
  assert.equal(watchingText(6, 2), "Watching 6 things, 2 new signals");
  assert.equal(watchingText(0, 1), "1 new signal");
});
