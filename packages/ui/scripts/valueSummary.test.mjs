import assert from "node:assert/strict";
import test from "node:test";
import { deltaLabel, headline, visibleLines } from "../src/lib/valueSummary.ts";

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

test("deltaLabel compares with the period before", () => {
  assert.equal(deltaLabel(5, 2), "+3");
  assert.equal(deltaLabel(1, 4), "−3");
  assert.equal(deltaLabel(2, 2), "same");
  assert.equal(deltaLabel(0, 0), "");
});

test("visibleLines drops lines that are zero in both periods", () => {
  const out = visibleLines([line("a", 0), line("b", 0, 2), line("c", 1)]);
  assert.deepEqual(out.map((l) => l.key), ["b", "c"]);
});
