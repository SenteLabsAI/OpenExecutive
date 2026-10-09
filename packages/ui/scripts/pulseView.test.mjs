import assert from "node:assert/strict";
import test from "node:test";
import { clockLabel, isMemoryView, pulseViewFor, upcomingLabel, whenLabel } from "../src/lib/pulseView.ts";

// Local-time constructors, so the labels hold in any test-runner time zone.
const NOW = new Date(2026, 9, 9, 14, 0); // Fri Oct 9 2026, 2 pm local
const at = (...args) => new Date(...args).toISOString();

test("pulseViewFor keeps the old tab links working", () => {
  assert.equal(pulseViewFor(null), "overview");
  assert.equal(pulseViewFor("heartbeat"), "overview");
  assert.equal(pulseViewFor("memory"), "overview");
  assert.equal(pulseViewFor("nonsense"), "overview");
  assert.equal(pulseViewFor("rhythm"), "schedule");
  assert.equal(pulseViewFor("followups"), "schedule");
  assert.equal(pulseViewFor("schedule"), "schedule");
  assert.equal(pulseViewFor("activity"), "activity");
  assert.equal(pulseViewFor("corrections"), "corrections");
  assert.equal(pulseViewFor("history"), "history");
});

test("isMemoryView picks out the memory lists", () => {
  assert.equal(isMemoryView("decisions"), true);
  assert.equal(isMemoryView("history"), true);
  assert.equal(isMemoryView("schedule"), false);
  assert.equal(isMemoryView("overview"), false);
});

test("clockLabel drops :00 and uses am/pm", () => {
  assert.equal(clockLabel(new Date(2026, 9, 9, 9, 0)), "9\u00a0am");
  assert.equal(clockLabel(new Date(2026, 9, 9, 18, 30)), "6:30\u00a0pm");
  assert.equal(clockLabel(new Date(2026, 9, 9, 0, 5)), "12:05\u00a0am");
  assert.equal(clockLabel(new Date(2026, 9, 9, 12, 0)), "12\u00a0pm");
});

test("whenLabel says today's time, Yesterday, a weekday, or a date", () => {
  assert.equal(whenLabel(at(2026, 9, 9, 18, 0), NOW), "6\u00a0pm");
  assert.equal(whenLabel(at(2026, 9, 9, 7, 0), NOW), "7\u00a0am");
  assert.equal(whenLabel(at(2026, 9, 8, 22, 0), NOW), "Yesterday");
  assert.equal(whenLabel(at(2026, 9, 10, 7, 0), NOW), "Sat 7\u00a0am");
  assert.equal(whenLabel(at(2026, 9, 12, 10, 0), NOW), "Mon 10\u00a0am");
  assert.equal(whenLabel(at(2026, 9, 6, 10, 0), NOW), "Tue");
  assert.equal(whenLabel(at(2026, 9, 1, 10, 0), NOW), "Oct 1");
  assert.equal(whenLabel(at(2026, 9, 20, 10, 0), NOW), "Oct 20");
  assert.equal(whenLabel("not a date", NOW), "");
});

test("upcomingLabel says Now for anything already due", () => {
  assert.equal(upcomingLabel(at(2026, 9, 9, 13, 0), NOW), "Now");
  assert.equal(upcomingLabel(at(2026, 9, 9, 18, 0), NOW), "6\u00a0pm");
});
