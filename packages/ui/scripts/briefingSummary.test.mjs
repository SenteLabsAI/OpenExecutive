import assert from "node:assert/strict";
import test from "node:test";
import {
  ageLabel,
  briefingSummary,
  daysUntil,
  greeting,
  proposalStatusChip,
} from "../src/lib/briefingSummary.ts";

// Local-time constructors, so the labels hold in any test-runner time zone.
const at = (h) => new Date(2026, 9, 3, h, 30);

test("greeting follows the time of day and drops a missing name", () => {
  assert.equal(greeting("Jordan", at(8)), "Good morning, Jordan");
  assert.equal(greeting("Jordan", at(13)), "Good afternoon, Jordan");
  assert.equal(greeting("Jordan", at(19)), "Good evening, Jordan");
  assert.equal(greeting(undefined, at(8)), "Good morning");
  assert.equal(greeting("  ", at(8)), "Good morning");
});

const zero = {
  needsYou: 0,
  handledOvernight: 0,
  peopleOverdue: 0,
  peopleNeedReply: 0,
  deptAtRisk: 0,
  inFlight: 0,
  monitoring: 0,
};

test("an all-clear day has no summary phrases", () => {
  assert.deepEqual(briefingSummary(zero), []);
});

test("the summary leads with what needs you, then what was handled", () => {
  const parts = briefingSummary({ ...zero, needsYou: 3, handledOvernight: 12, inFlight: 7, deptAtRisk: 1 });
  assert.deepEqual(
    parts.map((p) => p.text),
    ["3 things need you", "The Executive handled 12 overnight", "1 department at risk", "7 under way"],
  );
  assert.deepEqual(parts[0].target, { kind: "needsYou" });
  assert.deepEqual(parts[1].target, { kind: "panel", panel: "handled" });
});

test("singular wording, people phrases and the solo goals link", () => {
  const parts = briefingSummary({
    ...zero,
    needsYou: 1,
    peopleOverdue: 2,
    peopleNeedReply: 1,
    goalsAtRisk: 1,
    monitoring: 1,
  });
  assert.deepEqual(
    parts.map((p) => p.text),
    ["1 thing needs you", "2 overdue", "1 awaiting reply", "1 goal at risk", "1 signal monitored"],
  );
  assert.deepEqual(parts[1].target, { kind: "panel", panel: "people" });
  assert.deepEqual(parts[3].target, { kind: "href", href: "/goals" });
});

test("ageLabel and daysUntil", () => {
  const now = new Date(2026, 9, 3, 12, 0);
  assert.equal(ageLabel(new Date(2026, 9, 3, 11, 30).toISOString(), now), "30m");
  assert.equal(ageLabel(new Date(2026, 9, 2, 12, 0).toISOString(), now), "24h");
  assert.equal(ageLabel(new Date(2026, 8, 24, 12, 0).toISOString(), now), "9d");
  assert.equal(ageLabel("nonsense", now), "");
  assert.equal(daysUntil(new Date(2026, 9, 3, 1, 0).toISOString(), now), -1);
  assert.equal(daysUntil(new Date(2026, 9, 3, 20, 0).toISOString(), now), 0);
  assert.equal(daysUntil(null, now), null);
});

test("a collapsed card keeps one status chip: deadline, then stale, then age", () => {
  const now = new Date(2026, 9, 3, 12, 0);
  const created = new Date(2026, 9, 1, 12, 0).toISOString();
  assert.deepEqual(
    proposalStatusChip({ created_at: created, due_at: new Date(2026, 9, 2, 12, 0).toISOString() }, now),
    { label: "Overdue", tone: "rose" },
  );
  assert.deepEqual(
    proposalStatusChip({ created_at: created, due_at: new Date(2026, 9, 6, 13, 0).toISOString() }, now),
    { label: "Due in 3d", tone: "amber" },
  );
  assert.deepEqual(
    proposalStatusChip({ created_at: created, review_verdict: "likely_stale" }, now),
    { label: "Likely stale", tone: "amber" },
  );
  assert.deepEqual(proposalStatusChip({ created_at: created }, now), { label: "2d old", tone: "neutral" });
  assert.equal(proposalStatusChip({ created_at: "" }, now), null);
});
