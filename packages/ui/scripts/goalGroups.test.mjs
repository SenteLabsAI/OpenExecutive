import assert from "node:assert/strict";
import test from "node:test";
import { goalCounts, groupGoals } from "../src/lib/goalGroups.ts";

const goal = (id, { dept = null, deptTitle = null, owner = 1, ownerName = "Rufus", status = "on_track" } = {}) => ({
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
  last_reviewed_at: "",
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
  assert.deepEqual(goalCounts(GOALS), { on_track: 2, at_risk: 1, off_track: 1 });
  assert.deepEqual(groupGoals([], "owner"), []);
});
