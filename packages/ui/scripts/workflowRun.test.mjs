import assert from "node:assert/strict";
import test from "node:test";
import {
  runHref,
  shownSteps,
  startsOnArrival,
  stepCount,
} from "../src/lib/workflowRun.ts";

const steps = [
  { id: "context", title: "Load context" },
  { id: "cart", title: "Add items to the cart" },
  { id: "report", title: "Report" },
];

test("the setup step is never shown", () => {
  assert.deepEqual(
    shownSteps(steps).map((s) => s.id),
    ["cart", "report"],
  );
});

test("every screen counts the same steps", () => {
  assert.equal(stepCount(steps), "2 steps");
  assert.equal(stepCount([{ id: "context" }, { id: "only" }]), "1 step");
  assert.equal(stepCount([{ id: "a" }, { id: "b" }]), "2 steps");
});

test("Run on a list opens the workflow and starts it", () => {
  assert.equal(runHref("dog food"), "/jobs/dog%20food?run=1");
});

test("the run starts on arrival only when asked and nothing is left to fill in", () => {
  assert.equal(startsOnArrival("1", true), true);
  assert.equal(startsOnArrival("1", false), false);
  assert.equal(startsOnArrival(null, true), false);
  assert.equal(startsOnArrival("0", true), false);
});
