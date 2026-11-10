import assert from "node:assert/strict";
import { test } from "node:test";

import { nextLabel, watchingLine } from "../src/lib/watchingNext.ts";

const now = new Date(2026, 10, 10, 15, 0); // Tue Nov 10, 3 pm local

test("says when it acts next in plain words", () => {
  assert.equal(nextLabel(new Date(2026, 10, 10, 17, 30).toISOString(), now), "today at 5:30 pm");
  assert.equal(nextLabel(new Date(2026, 10, 11, 9, 0).toISOString(), now), "tomorrow at 9 am");
  assert.equal(nextLabel(new Date(2026, 10, 13, 9, 0).toISOString(), now), "on Friday at 9 am");
  assert.equal(nextLabel(new Date(2026, 10, 30, 9, 0).toISOString(), now), "on Nov 30");
  assert.equal(nextLabel(new Date(2026, 10, 10, 14, 0).toISOString(), now), "soon");
  assert.equal(nextLabel(null, now), "");
  assert.equal(nextLabel("nope", now), "");
});

test("the line joins what it does with when", () => {
  const at = new Date(2026, 10, 11, 9, 0).toISOString();
  assert.equal(watchingLine({ detail: "I follow up with Priya Nair", at }, now), "I follow up with Priya Nair tomorrow at 9 am");
  assert.equal(watchingLine({ detail: "Standing job", at: null }, now), "Standing job");
});
