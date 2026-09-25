// This month's AI spending and the monthly limit: the shape of the API's
// GET /audit/spending answer, and the wording the Token usage page,
// Settings and the Briefing's notice share.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/spending.test.mjs).

export type SpendingState = "no_limit" | "ok" | "near" | "reached";

/** Mirrors `SpendingResponse` in packages/core/openexecutive/api/routes/audit.py. */
export interface SpendingSummary {
  /** "2026-09": the month in the user's time zone (`zone`). */
  month: string;
  zone: string;
  /** Estimated: charges a provider reported, the rest at list price. */
  spent_usd: number;
  /** Month-end spend at this month's pace; null in the first three days. */
  forecast_usd: number | null;
  limit_usd: number | null;
  /** Calls of a model with no known price: not in `spent_usd`. */
  unpriced_calls: number;
  /** "near" from 80% of the limit. */
  state: SpendingState;
  /** Background work is paused because the limit was reached. */
  paused_for_budget: boolean;
  /** Whether this viewer may change the limit (the owner). */
  can_change_limit: boolean;
}

// Mirror BUDGET_MIN_USD / BUDGET_MAX_USD in memory/workspace_settings.py.
export const LIMIT_MIN_USD = 1;
export const LIMIT_MAX_USD = 1_000_000;

/** "$1,234.56". */
export function formatUsd(amount: number): string {
  return amount.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

/** "September 2026" for "2026-09"; anything else comes back as it is. */
export function monthName(month: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(month);
  if (!m) return month;
  const first = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, 1));
  return first.toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** How much of the limit is used, 0 to 100; null without a limit. */
export function limitShare(s: Pick<SpendingSummary, "spent_usd" | "limit_usd">): number | null {
  if (s.limit_usd === null || s.limit_usd <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((s.spent_usd / s.limit_usd) * 100)));
}

/** "About $12.34 spent in September 2026, of your $50.00 limit." */
export function spendingHeadline(s: SpendingSummary): string {
  const spent = `About ${formatUsd(s.spent_usd)} spent in ${monthName(s.month)}`;
  return s.limit_usd === null
    ? `${spent}. No monthly limit is set.`
    : `${spent}, of your ${formatUsd(s.limit_usd)} limit.`;
}

/** The forecast, what the estimate leaves out, and the pause, as one line. */
export function spendingDetail(s: SpendingSummary): string {
  const parts = [
    s.forecast_usd === null
      ? "A month-end forecast shows after the first three days."
      : `On pace for about ${formatUsd(s.forecast_usd)} by the end of the month.`,
  ];
  if (s.unpriced_calls > 0) {
    const n = s.unpriced_calls;
    parts.push(`${n} call${n === 1 ? "" : "s"} used a model with no known price, so the real figure is higher.`);
  }
  if (s.paused_for_budget) {
    parts.push("Background work is paused until next month, or until you raise or remove the limit.");
  }
  return parts.join(" ");
}

export type LimitInput = { ok: true; value: number | null } | { ok: false; error: string };

/** The limit field: blank for no limit, else whole dollars and cents in range. */
export function parseLimit(text: string): LimitInput {
  const t = text.trim().replace(/^\$\s*/, "").replace(/,/g, "");
  if (!t) return { ok: true, value: null };
  if (!/^\d+(\.\d{1,2})?$/.test(t)) {
    return { ok: false, error: "Enter an amount in dollars, like 50 or 49.99." };
  }
  const value = Number(t);
  if (value < LIMIT_MIN_USD || value > LIMIT_MAX_USD) {
    return { ok: false, error: `The limit must be between ${formatUsd(LIMIT_MIN_USD)} and ${formatUsd(LIMIT_MAX_USD)}.` };
  }
  return { ok: true, value };
}

/**
 * The Briefing's heads-up for the owner, or null. At 80% of the limit it
 * warns; once the limit is reached the paused banner says so, so this only
 * speaks up if that pause isn't in place (a person's pause is, say).
 */
export function spendingNotice(s: SpendingSummary): string | null {
  if (!s.can_change_limit || s.limit_usd === null) return null;
  const used = `About ${formatUsd(s.spent_usd)} of your ${formatUsd(s.limit_usd)} monthly AI limit is used`;
  if (s.state === "near") return `${used}. Background work pauses when it's reached.`;
  if (s.state === "reached" && !s.paused_for_budget) {
    return `This month's AI spending reached your ${formatUsd(s.limit_usd)} limit, so background work pauses until next month.`;
  }
  return null;
}
