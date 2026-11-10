import assert from "node:assert/strict";
import test from "node:test";
import { raiseTheBar, triggerInWords } from "../src/lib/watchWords.ts";

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

test("triggerInWords says when a watch only wants big changes", () => {
  assert.equal(
    triggerInWords("rss", { keywords: ["pricing"], only_big: true }),
    "There's a new post that mentions “pricing”, and only when it is a big change",
  );
  assert.equal(triggerInWords("page_watch", { only_big: true }), "The page changes, and only when it is a big change");
  // A stock's own threshold already says how big.
  assert.equal(triggerInWords("stock", { only_big: true }), "It moves 5% or more in a day, up or down");
});

test("raiseTheBar doubles a stock threshold", () => {
  const r = raiseTheBar({ signal_type: "stock", trigger_json: { abs_change_pct_gte: 5 } });
  assert.deepEqual(r?.patch, { trigger: { abs_change_pct_gte: 10 } });
  assert.equal(r?.label, "Only moves of 10%+");
  const empty = raiseTheBar({ signal_type: "stock", trigger_json: {} });
  assert.deepEqual(empty?.patch, { trigger: { abs_change_pct_gte: 10 } });
  const small = raiseTheBar({ signal_type: "stock", trigger_json: { abs_change_pct_gte: 0.4 } });
  assert.deepEqual(small?.patch, { trigger: { abs_change_pct_gte: 1 } });
  assert.equal(raiseTheBar({ signal_type: "stock", trigger_json: { abs_change_pct_gte: 100 } }), null);
});

test("raiseTheBar asks for big changes only otherwise, through the trigger", () => {
  const r = raiseTheBar({ signal_type: "rss", trigger_json: { keywords: ["x"] } });
  assert.deepEqual(r?.patch, { trigger: { keywords: ["x"], only_big: true } });
  assert.equal(r?.label, "Only big changes");
  assert.equal(r?.done, "You'll only hear about big changes from this.");
  assert.equal(raiseTheBar({ signal_type: "query", trigger_json: { only_big: true } }), null);
});
