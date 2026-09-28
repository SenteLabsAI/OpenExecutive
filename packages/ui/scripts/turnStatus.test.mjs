import assert from "node:assert/strict";
import test from "node:test";
import {
  LABEL_WAIT_AFTER_MS,
  SHOW_ELAPSED_AFTER_MS,
  STALL_MS,
  formatElapsed,
  turnStatus,
} from "../src/lib/turnStatus.ts";

const base = {
  isLoading: true,
  hasText: false,
  isConsulting: false,
  activityLabel: null,
  inCommittee: false,
  msSinceTurnStart: 0,
  msSinceLastEvent: 0,
  fallbackLabel: "Working…",
};

test("nothing shows when no turn is running", () => {
  assert.deepEqual(turnStatus({ ...base, isLoading: false, isConsulting: true }), {
    show: false,
    label: null,
    elapsed: null,
  });
});

test("before any event, the dots show alone and then get the fallback label", () => {
  assert.deepEqual(turnStatus(base), { show: true, label: null, elapsed: null });
  const later = turnStatus({ ...base, msSinceTurnStart: LABEL_WAIT_AFTER_MS });
  assert.equal(later.label, "Working…");
});

test("a tool round shows its activity label", () => {
  const s = turnStatus({ ...base, isConsulting: true, activityLabel: "Consulting specialists…" });
  assert.equal(s.show, true);
  assert.equal(s.label, "Consulting specialists…");
});

test("a tool round after text has streamed still shows", () => {
  const s = turnStatus({
    ...base,
    hasText: true,
    isConsulting: true,
    activityLabel: "Searching the web…",
  });
  assert.equal(s.show, true);
  assert.equal(s.label, "Searching the web…");
});

test("a tool round with no label uses the fallback", () => {
  assert.equal(turnStatus({ ...base, isConsulting: true }).label, "Working…");
});

test("flowing text hides the status; stalled text brings it back", () => {
  const flowing = turnStatus({ ...base, hasText: true, msSinceLastEvent: STALL_MS - 1 });
  assert.equal(flowing.show, false);
  const stalled = turnStatus({ ...base, hasText: true, msSinceLastEvent: STALL_MS });
  assert.deepEqual(stalled, { show: true, label: "Working…", elapsed: null });
});

test("committee review shows without a label, even after text", () => {
  const s = turnStatus({ ...base, hasText: true, inCommittee: true, isConsulting: true });
  assert.equal(s.show, true);
  assert.equal(s.label, null);
});

test("elapsed time appears only once the turn has run a while", () => {
  assert.equal(
    turnStatus({ ...base, msSinceTurnStart: SHOW_ELAPSED_AFTER_MS - 1 }).elapsed,
    null,
  );
  assert.equal(turnStatus({ ...base, msSinceTurnStart: 42_300 }).elapsed, "42s");
});

test("formatElapsed", () => {
  assert.equal(formatElapsed(0), "0s");
  assert.equal(formatElapsed(-5), "0s");
  assert.equal(formatElapsed(59_999), "59s");
  assert.equal(formatElapsed(60_000), "1m 00s");
  assert.equal(formatElapsed(65_000), "1m 05s");
  assert.equal(formatElapsed(3_725_000), "62m 05s");
});
