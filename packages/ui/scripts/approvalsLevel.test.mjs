import assert from "node:assert/strict";
import test from "node:test";
import { APPROVALS_LEVELS, approvalsTitle } from "../src/lib/approvalsLevel.ts";

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
