import { limitShare, spendingDetail, spendingHeadline, type SpendingSummary } from "@/lib/spending";

const BAR_COLOR: Record<SpendingSummary["state"], string> = {
  no_limit: "bg-indigo-500/60",
  ok: "bg-emerald-500",
  near: "bg-amber-400",
  reached: "bg-red-500",
};

/** This month's estimated AI spending: a sentence, a bar against the limit
 *  when there is one, and the forecast. Settings and Token usage share it. */
export default function SpendingMeter({ spending }: { spending: SpendingSummary }) {
  const share = limitShare(spending);
  return (
    <div>
      <p className="text-sm text-fg">{spendingHeadline(spending)}</p>
      {share !== null && (
        <div
          role="meter"
          aria-label="Share of the monthly limit used"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={share}
          className="mt-2 h-1.5 rounded bg-surface-input/60 overflow-hidden"
        >
          <div className={`h-full ${BAR_COLOR[spending.state]}`} style={{ width: `${share}%` }} />
        </div>
      )}
      <p className="mt-1.5 text-xs text-fg-muted leading-relaxed">{spendingDetail(spending)}</p>
    </div>
  );
}
