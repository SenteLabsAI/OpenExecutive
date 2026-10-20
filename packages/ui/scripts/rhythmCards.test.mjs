import assert from "node:assert/strict";
import test from "node:test";
import {
  reviewExcerpt,
  reviewRanLabel,
  topThreeSlot,
  topThreeWhy,
} from "../src/lib/rhythmCards.ts";

// Local-time constructors, so the labels hold in any test-runner time zone.
const NOW = new Date(2026, 8, 25, 10, 0); // Fri Sep 25 2026, 10:00 local

test("topThreeWhy capitalises the reason, writes its dates plainly, and tolerates an empty one", () => {
  assert.equal(topThreeWhy({ why: "overdue (was due 2026-09-23)" }), "Overdue (was due Sep 23)");
  assert.equal(topThreeWhy({ why: "due 2026-10-01" }), "Due Oct 1");
  // Not a real month: left as written.
  assert.equal(topThreeWhy({ why: "due 2026-13-01" }), "Due 2026-13-01");
  assert.equal(
    topThreeWhy({ why: "at risk; target 3 months, now 2.1 months" }),
    "At risk; target 3 months, now 2.1 months",
  );
  assert.equal(topThreeWhy({ why: "  " }), "");
});

test("topThreeSlot shows the block, a full day, or nothing without a calendar", () => {
  assert.equal(topThreeSlot({ slot: "10:00–11:00" }), "10:00–11:00");
  assert.equal(topThreeSlot({ slot: "" }), "No free time left today");
  assert.equal(topThreeSlot({ slot: null }), "");
});

test("reviewRanLabel reads today, yesterday and a weekday", () => {
  assert.equal(reviewRanLabel(new Date(2026, 8, 25, 8, 0).toISOString(), NOW), "Ran today");
  assert.equal(reviewRanLabel(new Date(2026, 8, 24, 16, 2).toISOString(), NOW), "Ran yesterday");
  assert.equal(reviewRanLabel(new Date(2026, 8, 18, 16, 2).toISOString(), NOW), "Ran Fri, Sep 18");
  assert.equal(reviewRanLabel("not a date", NOW), "");
});

test("reviewExcerpt prefers next week's top three, else the excerpt lines", () => {
  assert.deepEqual(
    reviewExcerpt({ top_three: ["Send the invoice — overdue", " ", "Close Northwind"], excerpt: "" }),
    {
      heading: "Next week's top three",
      numbered: true,
      lines: ["Send the invoice — overdue", "Close Northwind"],
    },
  );
  assert.deepEqual(
    reviewExcerpt({ top_three: [], excerpt: "Nothing stands out — a good week to get ahead." }),
    { heading: null, numbered: false, lines: ["Nothing stands out — a good week to get ahead."] },
  );
  assert.deepEqual(reviewExcerpt({ top_three: [], excerpt: "a\n\n b \n" }), {
    heading: null,
    numbered: false,
    lines: ["a", "b"],
  });
});
