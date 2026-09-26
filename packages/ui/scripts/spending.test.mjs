import assert from "node:assert/strict";
import test from "node:test";
import {
  formatUsd,
  limitShare,
  monthName,
  parseLimit,
  spendingDetail,
  spendingHeadline,
  spendingNotice,
} from "../src/lib/spending.ts";

const OWNER_VIEW = {
  month: "2026-09",
  zone: "America/Denver",
  spent_usd: 12.5,
  forecast_usd: null,
  limit_usd: 50,
  unpriced_calls: 0,
  state: "ok",
  paused_for_budget: false,
  can_change_limit: true,
};

test("amounts and months read the way people say them", () => {
  assert.equal(formatUsd(1234.5), "$1,234.50");
  assert.equal(formatUsd(0), "$0.00");
  assert.equal(monthName("2026-09"), "September 2026");
  assert.equal(monthName("not a month"), "not a month");
});

test("the share of the limit is capped and absent without a limit", () => {
  assert.equal(limitShare(OWNER_VIEW), 25);
  assert.equal(limitShare({ spent_usd: 80, limit_usd: 50 }), 100);
  assert.equal(limitShare({ spent_usd: 5, limit_usd: null }), null);
});

test("the headline says the spend, the month and the limit", () => {
  assert.equal(spendingHeadline(OWNER_VIEW), "About $12.50 spent in September 2026, of your $50.00 limit.");
  assert.equal(
    spendingHeadline({ ...OWNER_VIEW, limit_usd: null, state: "no_limit" }),
    "About $12.50 spent in September 2026. No monthly limit is set.",
  );
});

test("the detail adds the forecast, what isn't counted and the pause", () => {
  assert.match(spendingDetail(OWNER_VIEW), /after the first three days/);
  const detail = spendingDetail({ ...OWNER_VIEW, forecast_usd: 61, unpriced_calls: 2, paused_for_budget: true });
  assert.match(detail, /^On pace for about \$61\.00 by the end of the month\./);
  assert.match(detail, /2 calls used a model with no known price/);
  assert.match(detail, /Background work is paused until next month/);
  assert.match(spendingDetail({ ...OWNER_VIEW, unpriced_calls: 1 }), /1 call used/);
});

test("the limit field takes dollars and cents in range, or blank for none", () => {
  assert.deepEqual(parseLimit(""), { ok: true, value: null });
  assert.deepEqual(parseLimit("  "), { ok: true, value: null });
  assert.deepEqual(parseLimit("50"), { ok: true, value: 50 });
  assert.deepEqual(parseLimit("$1,000.25"), { ok: true, value: 1000.25 });
  for (const bad of ["0", "0.99", "-5", "1000000.01", "ten", "1e3", "5.001"]) {
    assert.equal(parseLimit(bad).ok, false, bad);
  }
  assert.match(parseLimit("0").error, /between \$1\.00 and \$1,000,000\.00/);
});

test("the Briefing notice is for the owner, from 80% of the limit", () => {
  assert.equal(spendingNotice(OWNER_VIEW), null);
  assert.equal(spendingNotice({ ...OWNER_VIEW, state: "no_limit", limit_usd: null }), null);
  const near = { ...OWNER_VIEW, spent_usd: 41, state: "near" };
  assert.equal(
    spendingNotice(near),
    "About $41.00 of your $50.00 monthly AI limit is used. Background work pauses when it's reached.",
  );
  assert.equal(spendingNotice({ ...near, can_change_limit: false }), null);
  // Paused for the limit: the paused banner already says so.
  const reached = { ...OWNER_VIEW, spent_usd: 55, state: "reached" };
  assert.equal(spendingNotice({ ...reached, paused_for_budget: true }), null);
  assert.match(spendingNotice(reached), /reached your \$50\.00 limit/);
});
