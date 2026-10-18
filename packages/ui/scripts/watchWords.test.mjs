import assert from "node:assert/strict";
import test from "node:test";
import {
  cadenceInWords,
  loudnessInWords,
  raiseTheBar,
  triggerInWords,
} from "../src/lib/watchWords.ts";

test("triggerInWords says when a watch tells you", () => {
  // Same lines as test_plain_lines in packages/core/tests/unit/test_watchlist_describe.py.
  assert.equal(triggerInWords("stock", {}), "It moves 5% or more in a day, up or down");
  assert.equal(triggerInWords("stock", { abs_change_pct_gte: 8 }), "It moves 8% or more in a day, up or down");
  assert.equal(
    triggerInWords("edgar", {}),
    "They file a 8-K (major news), 10-K (annual report) or 10-Q (quarterly report)",
  );
  assert.equal(
    triggerInWords("rss", { keywords: ["pricing", "launch"] }),
    "There's a new post that mentions “pricing” or “launch”",
  );
  assert.equal(triggerInWords("page_watch", null), "The page changes");
  assert.equal(triggerInWords("query", { keywords: ["layoffs"] }), "A new result turns up that mentions “layoffs”");
  assert.equal(triggerInWords("mystery", {}), "Something new turns up");
});

test("cadence and loudness in words", () => {
  assert.equal(cadenceInWords("daily"), "Once a day");
  assert.equal(cadenceInWords("odd"), "odd");
  assert.equal(loudnessInWords("low", "urgent"), "Everything, even small changes");
  assert.equal(loudnessInWords("medium", "high"), "Only things that matter, and never as urgent");
});

test("raiseTheBar doubles a stock threshold", () => {
  const r = raiseTheBar({ signal_type: "stock", trigger_json: { abs_change_pct_gte: 5 }, severity_floor: "low" });
  assert.deepEqual(r?.patch, { trigger: { abs_change_pct_gte: 10 } });
  assert.equal(r?.label, "Only moves of 10%+");
  const empty = raiseTheBar({ signal_type: "stock", trigger_json: {}, severity_floor: "low" });
  assert.deepEqual(empty?.patch, { trigger: { abs_change_pct_gte: 10 } });
  const small = raiseTheBar({ signal_type: "stock", trigger_json: { abs_change_pct_gte: 0.4 }, severity_floor: "low" });
  assert.deepEqual(small?.patch, { trigger: { abs_change_pct_gte: 1 } });
  const max = raiseTheBar({ signal_type: "stock", trigger_json: { abs_change_pct_gte: 100 }, severity_floor: "low" });
  assert.deepEqual(max?.patch, { severity_floor: "medium" });
});

test("raiseTheBar raises the severity floor one step otherwise", () => {
  const r = raiseTheBar({ signal_type: "rss", trigger_json: { keywords: ["x"] }, severity_floor: "medium" });
  assert.deepEqual(r?.patch, { severity_floor: "high" });
  assert.equal(r?.label, "Only big things");
  assert.equal(r?.done, "You'll only hear about big things from this.");
  assert.equal(raiseTheBar({ signal_type: "query", trigger_json: {}, severity_floor: "urgent" }), null);
});
