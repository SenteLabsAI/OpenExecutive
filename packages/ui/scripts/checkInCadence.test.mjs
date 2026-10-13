import assert from "node:assert/strict";
import test from "node:test";
import {
  checkInSpec,
  describeCheckIn,
  formatNextCheckIn,
  parseCheckIn,
  withKind,
} from "../src/lib/checkInCadence.ts";

test("parseCheckIn reads daily, weekly and off specs", () => {
  assert.deepEqual(parseCheckIn("daily@09:00"), { kind: "daily", time: "09:00" });
  assert.deepEqual(parseCheckIn("weekly@mon@09:00"), { kind: "weekly", day: "mon", time: "09:00" });
  assert.deepEqual(parseCheckIn("weekly@Fri-16:30"), { kind: "weekly", day: "fri", time: "16:30" });
  assert.deepEqual(parseCheckIn(""), { kind: "off" });
  assert.deepEqual(parseCheckIn("  "), { kind: "off" });
  assert.deepEqual(parseCheckIn(undefined), { kind: "off" });
});

test("parseCheckIn keeps a spec the picker can't show", () => {
  assert.deepEqual(parseCheckIn("quarterly@01-09:00"), { kind: "custom", spec: "quarterly@01-09:00" });
  assert.deepEqual(parseCheckIn("daily@25:00"), { kind: "custom", spec: "daily@25:00" });
  assert.deepEqual(parseCheckIn("weekly@xyz@09:00"), { kind: "custom", spec: "weekly@xyz@09:00" });
});

test("checkInSpec writes what the backend parses; off is empty", () => {
  assert.equal(checkInSpec({ kind: "daily", time: "14:00" }), "daily@14:00");
  assert.equal(checkInSpec({ kind: "weekly", day: "thu", time: "08:15" }), "weekly@thu@08:15");
  assert.equal(checkInSpec({ kind: "off" }), "");
  assert.equal(checkInSpec({ kind: "custom", spec: "quarterly@01-09:00" }), "quarterly@01-09:00");
  // Round trip.
  for (const spec of ["daily@07:45", "weekly@sun@23:59", "", "quarterly@15-10:00"]) {
    assert.equal(checkInSpec(parseCheckIn(spec)), spec);
  }
});

test("withKind keeps the time and day already picked", () => {
  const weekly = { kind: "weekly", day: "wed", time: "10:30" };
  assert.deepEqual(withKind(weekly, "daily"), { kind: "daily", time: "10:30" });
  assert.deepEqual(withKind({ kind: "daily", time: "10:30" }, "weekly"), { kind: "weekly", day: "mon", time: "10:30" });
  assert.deepEqual(withKind(weekly, "weekly"), weekly);
  assert.deepEqual(withKind(weekly, "off"), { kind: "off" });
  // From off or a custom spec, the new-department default: Monday 09:00.
  assert.deepEqual(withKind({ kind: "off" }, "weekly"), { kind: "weekly", day: "mon", time: "09:00" });
  assert.deepEqual(withKind({ kind: "custom", spec: "quarterly@01-09:00" }, "daily"), { kind: "daily", time: "09:00" });
});

test("describeCheckIn says the schedule in words", () => {
  assert.equal(describeCheckIn(parseCheckIn("weekly@mon@09:00")), "Every Monday at 09:00 UTC");
  assert.equal(describeCheckIn(parseCheckIn("daily@14:00")), "Every day at 14:00 UTC");
  assert.equal(describeCheckIn(parseCheckIn("")), "Off");
  assert.equal(describeCheckIn(parseCheckIn("quarterly@01-09:00")), "quarterly@01-09:00");
});

test("formatNextCheckIn shows the queued time in the given zone", () => {
  const text = formatNextCheckIn("2026-10-19T09:00:00+00:00", "UTC", "en-GB");
  assert.match(text, /Mon/);
  assert.match(text, /19/);
  assert.match(text, /09:00/);
  assert.match(formatNextCheckIn("2026-10-19T09:00:00+00:00", "America/New_York", "en-GB"), /05:00/);
  assert.equal(formatNextCheckIn(null), "");
  assert.equal(formatNextCheckIn("not a date"), "");
  // An unknown zone falls back to the default rather than throwing.
  assert.notEqual(formatNextCheckIn("2026-10-19T09:00:00+00:00", "Not/AZone", "en-GB"), "");
});
