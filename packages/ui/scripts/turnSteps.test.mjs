import assert from "node:assert/strict";
import test from "node:test";
import { MAX_STEPS, addStep, applyPlan, planRows, showSteps } from "../src/lib/turnSteps.ts";

test("each new label is a step, a repeat of the last is not", () => {
  let steps = addStep([], "Reading the hiring plan");
  steps = addStep(steps, "Reading the hiring plan");
  steps = addStep(steps, "Checking your calendar");
  steps = addStep(steps, "Reading the hiring plan");
  assert.deepEqual(steps, [
    "Reading the hiring plan",
    "Checking your calendar",
    "Reading the hiring plan",
  ]);
});

test("blank labels are skipped", () => {
  assert.deepEqual(addStep([], null), []);
  assert.deepEqual(addStep(["a"], "  "), ["a"]);
});

test("only the newest steps are kept", () => {
  let steps = [];
  for (let i = 0; i < MAX_STEPS + 3; i++) steps = addStep(steps, `step ${i}`);
  assert.equal(steps.length, MAX_STEPS);
  assert.equal(steps[steps.length - 1], `step ${MAX_STEPS + 2}`);
});

test("a one-step turn keeps the one-line status", () => {
  assert.equal(showSteps(["a"]), false);
  assert.equal(showSteps(["a", "b"]), true);
});

test("a plan shows its steps with the current one", () => {
  const plan = applyPlan(null, ["Find the deals", "Read the emails", "Write it up"], 1);
  assert.deepEqual(
    planRows(plan).map((r) => [r.label, r.state, r.added]),
    [
      ["Find the deals", "done", false],
      ["Read the emails", "current", false],
      ["Write it up", "todo", false],
    ],
  );
});

test("a step added after the first plan is marked added", () => {
  let plan = applyPlan(null, ["A", "B", "C"], 0);
  plan = applyPlan(plan, ["A", "B", "New", "C"], 2);
  assert.deepEqual(planRows(plan).map((r) => r.added), [false, false, true, false]);
  assert.equal(planRows(plan)[2].state, "current");
});

test("an empty plan keeps the one before, and current is clamped", () => {
  const plan = applyPlan(null, ["A", "B"], 9);
  assert.equal(plan.current, 1);
  assert.equal(applyPlan(plan, [], 0), plan);
  assert.equal(applyPlan(null, "nope", 0), null);
  assert.equal(applyPlan(null, ["A", "B"], "x").current, 0);
});

test("writing the reply moves to the last step; handing over runs none", () => {
  const plan = applyPlan(null, ["A", "B", "C"], 1);
  assert.deepEqual(planRows(plan, { writing: true }).map((r) => r.state), ["done", "done", "current"]);
  // Text before an early step's calls is not the reply.
  const early = applyPlan(null, ["A", "B", "C", "D"], 0);
  assert.deepEqual(planRows(early, { writing: true }).map((r) => r.state), ["current", "todo", "todo", "todo"]);
  assert.deepEqual(planRows(plan, { handingOver: true }).map((r) => r.state), ["done", "todo", "todo"]);
});
