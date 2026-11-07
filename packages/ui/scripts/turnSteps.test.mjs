import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_STEPS,
  addReads,
  addStep,
  applyProgress,
  detailsSummary,
  firstFew,
  newestFew,
  readChips,
  showSteps,
} from "../src/lib/turnSteps.ts";

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

test("each progress event replaces the last; an empty one keeps it", () => {
  let p = applyProgress(null, { task: " Reviewing the LOI ", now: "Reading it", used: ["LOI.pdf", "", 3] });
  assert.deepEqual(p, { task: "Reviewing the LOI", now: "Reading it", used: ["LOI.pdf"], found: [] });
  p = applyProgress(p, { task: "Reviewing the LOI", now: "Checking rent", used: ["LOI.pdf", "Rent.xlsx"], found: ["No cap"] });
  assert.equal(p.now, "Checking rent");
  assert.deepEqual(p.found, ["No cap"]);
  assert.equal(applyProgress(p, { now: " ", used: [] }), p);
  assert.equal(applyProgress(null, {}), null);
});

test("the card stays the same size: first sources, newest findings", () => {
  assert.deepEqual(firstFew(["a", "b", "c", "d", "e"], 3), { shown: ["a", "b", "c"], more: 2 });
  assert.deepEqual(firstFew(["a"], 3), { shown: ["a"], more: 0 });
  assert.deepEqual(newestFew(["1", "2", "3", "4"], 3), { shown: ["2", "3", "4"], earlier: 1 });
  assert.deepEqual(newestFew([], 3), { shown: [], earlier: 0 });
});

test("reads add up by kind and read as chips", () => {
  let reads = addReads({}, { email: 2, file: 1 });
  reads = addReads(reads, { email: 1, person: 2, calendar: 1, bad: "x", zero: 0 });
  reads = addReads(reads, undefined);
  assert.deepEqual(reads, { email: 3, file: 1, person: 2, calendar: 1 });
  assert.deepEqual(readChips(reads), ["3 emails", "1 file", "2 people", "Your calendar"]);
  const same = {};
  assert.equal(addReads(same, {}), same);
});

test("the details link counts sources and findings", () => {
  assert.equal(detailsSummary(null, {}), null);
  assert.equal(
    detailsSummary({ task: "", now: "", used: ["a", "b"], found: ["x"] }, { file: 9 }),
    "2 sources · 1 found",
  );
  assert.equal(detailsSummary(null, { email: 3, file: 1 }), "4 sources");
  assert.equal(detailsSummary({ task: "", now: "", used: [], found: ["x", "y"] }, { file: 1 }), "1 source · 2 found");
});
