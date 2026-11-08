import assert from "node:assert/strict";
import test from "node:test";
import { APPROVALS_LEVELS, approvalsTitle, trainingNote } from "../src/lib/approvalsLevel.ts";

test("the three levels, in order", () => {
  assert.deepEqual(
    APPROVALS_LEVELS.map((l) => l.title),
    ["Careful", "Balanced", "Hands-off"],
  );
});

test("a level's name, and Careful for anything unknown", () => {
  assert.equal(approvalsTitle("outsiders"), "Balanced");
  assert.equal(approvalsTitle("trust"), "Hands-off");
  assert.equal(approvalsTitle("nope"), "Careful");
});

test("the training line shows only while Take the lead is on and in training", () => {
  assert.equal(trainingNote(null), null);
  assert.equal(trainingNote({ enabled: false, training: true }), null);
  assert.equal(trainingNote({ enabled: true, training: false }), null);
  assert.match(trainingNote({ enabled: true, training: true }), /still waits for you/);
});
