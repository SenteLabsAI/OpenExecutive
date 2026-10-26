import assert from "node:assert/strict";
import test from "node:test";
import {
  clockLabel,
  crossesMidnight,
  digestDescription,
  heldLine,
  quietError,
  quietSummary,
  rightNowDescription,
  tileStatus,
  toUpdate,
} from "../src/lib/notifications.ts";

const NIGHT = { enabled: true, start: "22:00", end: "07:00", days: "every_day" };

test("times read the way people say them", () => {
  assert.equal(clockLabel("22:00"), "10:00 pm");
  assert.equal(clockLabel("07:05"), "7:05 am");
  assert.equal(clockLabel("00:30"), "12:30 am");
  assert.equal(clockLabel("12:00"), "12:00 pm");
});

test("quiet hours say what they do", () => {
  assert.equal(quietSummary({ ...NIGHT, enabled: false }), "Off. Messages can reach you at any hour.");
  assert.equal(quietSummary(NIGHT), "Quiet from 10:00 pm to 7:00 am the next morning, every day.");
  assert.equal(
    quietSummary({ ...NIGHT, days: "weekdays" }),
    "Quiet from 10:00 pm to 7:00 am the next morning on weekdays, and all weekend.",
  );
  assert.equal(
    quietSummary({ ...NIGHT, start: "12:00", end: "13:00" }),
    "Quiet from 12:00 pm to 1:00 pm, every day.",
  );
  assert.ok(crossesMidnight(NIGHT));
  assert.ok(!crossesMidnight({ start: "12:00", end: "13:00" }));
});

test("a quiet window that starts when it ends can't be saved", () => {
  assert.equal(quietError(NIGHT), null);
  assert.equal(quietError({ ...NIGHT, end: "22:00" }), "Quiet hours need to start and end at different times.");
  assert.equal(quietError({ ...NIGHT, start: "" }), "Pick a start and end time.");
  assert.equal(quietError({ ...NIGHT, enabled: false, start: "" }), null);
});

test("In my briefs is worded for whether they get the briefs", () => {
  assert.match(digestDescription(true), /morning brief or end-of-day digest/);
  assert.match(digestDescription(false), /when the morning and end-of-day briefs go out/);
});

test("waiting messages are counted only when there are some", () => {
  assert.equal(heldLine(0, "digest"), null);
  assert.equal(heldLine(1, "digest"), "1 message is waiting for your next brief.");
  assert.equal(heldLine(3, "instant"), "3 messages are waiting for your quiet hours to end.");
});

test("saving fills an unset zone from the browser, never overwrites a chosen one", () => {
  const base = { quiet_hours: NIGHT, delivery: "instant", urgent_through: true };
  assert.equal(toUpdate({ ...base, timezone: "" }, "Europe/London").timezone, "Europe/London");
  assert.equal(toUpdate({ ...base, timezone: "Asia/Tokyo" }, "Europe/London").timezone, "Asia/Tokyo");
  assert.equal(toUpdate({ ...base, timezone: "" }, null).timezone, "");
  assert.deepEqual(Object.keys(toUpdate({ ...base, timezone: "" }, null)).sort(), [
    "delivery",
    "quiet_hours",
    "timezone",
    "urgent_through",
  ]);
});

test("the hub tile sums it up", () => {
  assert.equal(tileStatus({ quiet_hours: NIGHT, delivery: "digest" }), "Quiet 10:00 pm–7:00 am · In my briefs");
  assert.equal(
    tileStatus({ quiet_hours: { ...NIGHT, enabled: false }, delivery: "instant" }),
    "Quiet hours off · As they happen",
  );
});

test("the Right now choice is sent only when the backend has one", () => {
  const base = { quiet_hours: NIGHT, delivery: "instant", urgent_through: true, timezone: "" };
  assert.equal(toUpdate({ ...base, right_now: "everything" }, null).right_now, "everything");
  assert.equal("right_now" in toUpdate(base, null), false);
  assert.match(rightNowDescription("important"), /needs you/);
  assert.match(rightNowDescription("off"), /^Nothing/);
});
