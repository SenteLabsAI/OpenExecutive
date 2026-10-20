import assert from "node:assert/strict";
import test from "node:test";
import { applyGoalChange, groupGoalsByArea } from "../src/lib/goalAreas.ts";

const goal = (id, slug, status = "on_track") => ({
  id,
  department_slug: slug,
  period_type: "quarter",
  period_value: "2026-Q4",
  key_result: `Goal ${id}`,
  target: "10",
  current: "",
  status,
  created_at: "",
  updated_at: "",
  last_reviewed_at: "",
});

const area = (slug, title, goals = []) => ({
  config: { slug, title },
  goals,
  headcount: null,
  budget_usd: null,
  member_person_ids: [],
  updated_at: "",
});

const DEPTS = [
  area("sales", "Sales", [goal(1, "sales"), goal(2, "sales", "at_risk")]),
  area("finance", "Finance"),
  area("marketing", "Marketing", [goal(3, "marketing", "off_track")]),
  area("engineering", "Engineering"),
];

test("areas with goals come first, in API order; the rest by title", () => {
  const g = groupGoalsByArea(DEPTS);
  assert.deepEqual(g.withGoals.map((a) => a.slug), ["sales", "marketing"]);
  assert.deepEqual(g.empty.map((a) => a.slug), ["engineering", "finance"]);
  assert.deepEqual(g.all.map((a) => a.title), ["Engineering", "Finance", "Marketing", "Sales"]);
  assert.deepEqual(g.counts, { on_track: 1, at_risk: 1, off_track: 1 });
});

test("no areas, or no goals, group to empty lists", () => {
  assert.deepEqual(groupGoalsByArea([]).withGoals, []);
  const g = groupGoalsByArea([area("ops", "Operations")]);
  assert.deepEqual(g.withGoals, []);
  assert.deepEqual(g.empty, [{ slug: "ops", title: "Operations" }]);
});

test("a saved goal is added to, or replaced in, its own area only", () => {
  const added = applyGoalChange(DEPTS, { saved: goal(9, "finance") });
  assert.deepEqual(added.find((d) => d.config.slug === "finance").goals.map((x) => x.id), [9]);
  assert.equal(added.find((d) => d.config.slug === "sales").goals.length, 2);

  const edited = applyGoalChange(DEPTS, { saved: { ...goal(2, "sales"), key_result: "Renamed" } });
  const sales = edited.find((d) => d.config.slug === "sales").goals;
  assert.deepEqual(sales.map((x) => x.key_result), ["Goal 1", "Renamed"]);
});

test("a deleted goal leaves its area", () => {
  const next = applyGoalChange(DEPTS, { deleted: { slug: "sales", id: 1 } });
  assert.deepEqual(next.find((d) => d.config.slug === "sales").goals.map((x) => x.id), [2]);
  assert.equal(groupGoalsByArea(next).withGoals.length, 2);
  const gone = applyGoalChange(next, { deleted: { slug: "marketing", id: 3 } });
  assert.deepEqual(groupGoalsByArea(gone).withGoals.map((a) => a.slug), ["sales"]);
});
