import assert from "node:assert/strict";
import test from "node:test";
import { goalCounts, groupGoals, shownStatus } from "../src/lib/goalGroups.ts";

const goal = (id, { dept = null, deptTitle = null, owner = 1, ownerName = "Rufus", status = "on_track", reviewed = "2026-11-02T09:00:00+00:00" } = {}) => ({
  id,
  department_slug: dept,
  department_title: deptTitle,
  owner_id: owner,
  owner_name: ownerName,
  period_type: "quarter",
  period_value: "Q4 2026",
  key_result: `Goal ${id}`,
  target: "10",
  current: "",
  status,
  created_at: "",
  updated_at: "",
  last_reviewed_at: reviewed,
});

const GOALS = [
  goal(3, { owner: 2, ownerName: "Dan", dept: "sales", deptTitle: "Sales", status: "at_risk" }),
  goal(1),
  goal(2, { owner: 3, ownerName: "Alex", status: "off_track" }),
  goal(4, { dept: "sales", deptTitle: "Sales" }),
];

test("by owner: the viewer first, then by name; goals oldest first", () => {
  const groups = groupGoals(GOALS, "owner", { viewerId: 1 });
  assert.deepEqual(groups.map((g) => g.title), ["Rufus (you)", "Alex", "Dan"]);
  assert.deepEqual(groups[0].goals.map((g) => g.id), [1, 4]);
});

test("by department: departments by title, goals with none last", () => {
  const groups = groupGoals(GOALS, "department");
  assert.deepEqual(groups.map((g) => g.title), ["Sales", "No department"]);
  assert.deepEqual(groups[1].goals.map((g) => g.id), [1, 2]);
  assert.equal(groupGoals(GOALS, "department", { unit: "area" })[1].title, "No area");
});

test("by status: worst first, empty statuses left out", () => {
  const groups = groupGoals(GOALS, "status");
  assert.deepEqual(groups.map((g) => g.title), ["Off track", "At risk", "On track"]);
  assert.deepEqual(groupGoals([goal(1)], "status").map((g) => g.title), ["On track"]);
});

test("a goal nobody owns groups last; counts per status", () => {
  const groups = groupGoals([goal(1, { owner: null, ownerName: "" }), goal(2)], "owner", { viewerId: 1 });
  assert.deepEqual(groups.map((g) => g.title), ["Rufus (you)", "No owner yet"]);
  assert.deepEqual(goalCounts(GOALS), { on_track: 2, at_risk: 1, off_track: 1, not_checked: 0, done: 0 });
  assert.deepEqual(groupGoals([], "owner"), []);
});

test("a goal never checked reads not checked yet, not on track", () => {
  assert.equal(shownStatus(goal(1, { reviewed: "" })), "not_checked");
  assert.equal(shownStatus(goal(1)), "on_track");
  // A status someone set stays shown, checked or not.
  assert.equal(shownStatus(goal(1, { status: "at_risk", reviewed: "" })), "at_risk");
  const goals = [...GOALS, goal(5, { reviewed: "" })];
  assert.deepEqual(goalCounts(goals), { on_track: 2, at_risk: 1, off_track: 1, not_checked: 1, done: 0 });
  assert.deepEqual(groupGoals(goals, "status").map((g) => g.title), ["Off track", "At risk", "Not checked yet", "On track"]);
});

test("a done goal leaves its group for Done, last under every grouping", () => {
  const goals = [...GOALS, goal(5, { dept: "sales", deptTitle: "Sales", status: "done" }), goal(6, { status: "done", reviewed: "" })];
  for (const by of ["owner", "department", "status"]) {
    const groups = groupGoals(goals, by, { viewerId: 1 });
    const last = groups[groups.length - 1];
    assert.equal(last.title, "Done");
    assert.deepEqual(last.goals.map((g) => g.id), [5, 6]);
    assert.ok(groups.slice(0, -1).every((g) => g.goals.every((x) => x.status !== "done")));
  }
  assert.equal(shownStatus(goals[5]), "done");
  assert.equal(goalCounts(goals).done, 2);
});
