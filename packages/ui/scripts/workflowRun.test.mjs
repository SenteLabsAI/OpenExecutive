import assert from "node:assert/strict";
import test from "node:test";
import {
  RUN_NOTE_MAX_AGE_MS,
  noteRunOnArrival,
  shownSteps,
  startsOnArrival,
  stepCount,
  takeRunOnArrival,
} from "../src/lib/workflowRun.ts";

const steps = [
  { id: "context", title: "Load context" },
  { id: "cart", title: "Add items to the cart" },
  { id: "report", title: "Report" },
];

function memoryStore() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    size: () => m.size,
  };
}

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

test("a fresh note from Run starts the run once", () => {
  const store = memoryStore();
  noteRunOnArrival("dog", store, 1_000);
  assert.equal(takeRunOnArrival("dog", store, 2_000), true);
  assert.equal(takeRunOnArrival("dog", store, 2_000), false, "a refresh doesn't run it again");
  assert.equal(store.size(), 0);
});

test("no note, another workflow's note, or a stale note doesn't start it", () => {
  const store = memoryStore();
  assert.equal(takeRunOnArrival("dog", store, 0), false);
  noteRunOnArrival("cat", store, 0);
  assert.equal(takeRunOnArrival("dog", store, 0), false);
  noteRunOnArrival("dog", store, 0);
  assert.equal(takeRunOnArrival("dog", store, RUN_NOTE_MAX_AGE_MS + 1), false);
  assert.equal(takeRunOnArrival("dog", store, RUN_NOTE_MAX_AGE_MS + 1), false, "a stale note is dropped");
});

test("blocked storage means one tap more, never a crash", () => {
  const broken = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
    removeItem: () => {},
  };
  noteRunOnArrival("dog", broken, 0);
  assert.equal(takeRunOnArrival("dog", broken, 0), false);
  assert.equal(takeRunOnArrival("dog", null, 0), false);
});

test("the run starts on arrival only when asked and nothing is left to fill in", () => {
  assert.equal(startsOnArrival(true, true), true);
  assert.equal(startsOnArrival(true, false), false);
  assert.equal(startsOnArrival(false, true), false);
});
