import assert from "node:assert/strict";
import test from "node:test";
import { snoozeChoices } from "../src/lib/snooze.ts";

// Local-time constructors, so the choices hold in any test-runner time zone.
const at = (day, hour, minute = 0) => new Date(2026, 9, day, hour, minute); // Oct 2026

test("a weekday afternoon offers later today, tomorrow and Monday", () => {
  const choices = snoozeChoices(at(13, 13, 20)); // Tue 13:20
  assert.equal(choices.length, 3);
  assert.deepEqual(choices[0].until, at(13, 16, 30));
  assert.deepEqual(choices[1].until, at(14, 9));
  assert.deepEqual(choices[2].until, at(19, 9));
  assert.match(choices[1].label, /^Snooze until tomorrow /);
});

test("late evening drops later today", () => {
  const choices = snoozeChoices(at(13, 22));
  assert.equal(choices.length, 2);
  assert.deepEqual(choices[0].until, at(14, 9));
});

test("Monday points at next Monday, Sunday skips it", () => {
  const monday = snoozeChoices(at(12, 10));
  assert.deepEqual(monday.at(-1).until, at(19, 9));
  const sunday = snoozeChoices(at(18, 10));
  assert.ok(sunday.every((c) => !c.label.includes("Monday")));
  assert.deepEqual(sunday.at(-1).until, at(19, 9));
});
