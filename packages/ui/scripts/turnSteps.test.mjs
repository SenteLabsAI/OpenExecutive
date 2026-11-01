import assert from "node:assert/strict";
import test from "node:test";
import { MAX_STEPS, addStep, showSteps } from "../src/lib/turnSteps.ts";

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
