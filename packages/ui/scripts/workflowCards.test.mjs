import assert from "node:assert/strict";
import test from "node:test";
import { cadenceLabel, cardAction, lastRunAt } from "../src/lib/workflowCards.ts";

test("cadenceLabel says the known cadences in words", () => {
  assert.equal(cadenceLabel("daily@08:30"), "Every day at 08:30 UTC");
  assert.equal(cadenceLabel("weekly@mon@09:00"), "Every Monday at 09:00 UTC");
  assert.equal(cadenceLabel("weekly@FRI@17:00"), "Every Friday at 17:00 UTC");
  assert.equal(cadenceLabel("quarterly@05-10:00"), "Quarterly, day 5 at 10:00 UTC");
});

test("cadenceLabel keeps an unknown or empty cadence readable", () => {
  assert.equal(cadenceLabel(null), "");
  assert.equal(cadenceLabel("  "), "");
  assert.equal(cadenceLabel("weekly@xyz@09:00"), "weekly@xyz@09:00");
  assert.equal(cadenceLabel("hourly"), "hourly");
});

const run = (id, name, status, updated) => ({
  run_id: id,
  workflow_name: name,
  status,
  updated_at: updated,
});

test("cardAction: off workflows are reviewed, sign-offs lead, else Run", () => {
  const runs = [
    run("a", "digest", "done", "2026-10-01T09:00:00Z"),
    run("b", "board", "awaiting_human", "2026-10-01T09:00:00Z"),
    run("c", "board", "awaiting_human", "2026-10-02T09:00:00Z"),
  ];
  assert.deepEqual(cardAction("digest", false, runs), { kind: "approve" });
  assert.deepEqual(cardAction("board", true, runs), { kind: "signoff", runId: "c" });
  assert.deepEqual(cardAction("digest", true, runs), { kind: "run" });
  assert.deepEqual(cardAction("other", true, []), { kind: "run" });
});

test("lastRunAt is the newest run of that workflow", () => {
  const runs = [
    run("a", "digest", "done", "2026-10-01T09:00:00Z"),
    run("b", "digest", "error", "2026-10-02T09:00:00Z"),
    run("c", "board", "done", "2026-10-03T09:00:00Z"),
  ];
  assert.equal(lastRunAt("digest", runs), "2026-10-02T09:00:00Z");
  assert.equal(lastRunAt("none", runs), null);
});
