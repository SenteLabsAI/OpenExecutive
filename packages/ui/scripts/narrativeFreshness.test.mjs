import assert from "node:assert/strict";
import test from "node:test";
import {
  FOCUS_REFRESH_MIN_GAP_MS,
  narrativeRepollDelay,
  narrativeUpdatedLabel,
  shouldRefreshOnFocus,
} from "../src/lib/narrativeFreshness.ts";

test("re-polls a few times while the header is rewritten, then stops", () => {
  assert.equal(narrativeRepollDelay(0), 4000);
  assert.equal(narrativeRepollDelay(1), 10000);
  assert.equal(narrativeRepollDelay(2), 20000);
  assert.equal(narrativeRepollDelay(3), null);
  assert.equal(narrativeRepollDelay(-1), null);
  assert.equal(narrativeRepollDelay(1.5), null);
});

test("focus refresh is throttled", () => {
  assert.equal(shouldRefreshOnFocus(0, FOCUS_REFRESH_MIN_GAP_MS - 1), false);
  assert.equal(shouldRefreshOnFocus(0, FOCUS_REFRESH_MIN_GAP_MS), true);
});

test("updated label reads as recency, then as a local clock time", () => {
  // Local-time constructors, so the labels hold in any test-runner time zone.
  const written = new Date(2026, 8, 28, 14, 4);
  const at = (minutes) => new Date(written.getTime() + minutes * 60_000);
  const iso = written.toISOString();
  assert.equal(narrativeUpdatedLabel(iso, at(0.5)), "Updated just now");
  assert.equal(narrativeUpdatedLabel(iso, at(12)), "Updated 12 min ago");
  assert.equal(narrativeUpdatedLabel(iso, at(90)), "Updated 2:04 PM");
  const morning = new Date(2026, 8, 28, 0, 7);
  assert.equal(
    narrativeUpdatedLabel(morning.toISOString(), new Date(2026, 8, 28, 9, 0)),
    "Updated 12:07 AM",
  );
  assert.equal(narrativeUpdatedLabel(null, at(0)), null);
  assert.equal(narrativeUpdatedLabel("not a date", at(0)), null);
});
