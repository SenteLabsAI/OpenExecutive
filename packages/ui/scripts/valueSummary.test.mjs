import assert from "node:assert/strict";
import test from "node:test";
import { headline, periodNames, visibleLines } from "../src/lib/valueSummary.ts";

const line = (key, count, previous = 0) => ({ key, label: key, count, previous });

test("headline names the first three non-zero lines in order", () => {
  const lines = [
    line("answered", 9),
    line("reports", 1),
    line("messages_sent", 42),
    line("meetings_booked", 0),
    line("follow_ups", 3),
  ];
  assert.equal(headline(lines), "42 messages sent, 1 report, 3 follow-ups");
  assert.equal(headline([line("reports", 0)]), "");
});

test("periodNames names the month shown and the one before", () => {
  assert.deepEqual(periodNames("month", "October 2026"), {
    current: "October",
    previous: "September",
    previousShort: "Sep",
    heading: "October",
  });
  assert.equal(periodNames("month", "January 2027").previous, "December");
});

test("periodNames names a week against the week before", () => {
  const names = periodNames("week", "Week of 12 October");
  assert.equal(names.current, "Week of 12 October");
  assert.equal(names.previousShort, "Week before");
  assert.equal(names.heading, "the week of 12 October");
});

test("visibleLines drops lines that are zero in both periods", () => {
  const out = visibleLines([line("a", 0), line("b", 0, 2), line("c", 1)]);
  assert.deepEqual(out.map((l) => l.key), ["b", "c"]);
});
