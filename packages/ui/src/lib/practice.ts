// Shared presentation helpers for practice-cockpit cards, used by both the
// /clients cockpit board and the briefing's "Across your clients" panel so
// the two surfaces can never drift.

import type { ClientCockpitCard } from "@/lib/api";

// Renewal badge thresholds (days until renewal_date).
export const RENEWAL_WARN_DAYS = 30;
export const RENEWAL_URGENT_DAYS = 7;

export function renewalBadge(
  daysToRenewal: number | null | undefined,
): { label: string; urgent: boolean } | null {
  if (typeof daysToRenewal !== "number" || daysToRenewal > RENEWAL_WARN_DAYS) {
    return null;
  }
  return {
    label: daysToRenewal <= 0 ? "renewal due" : `renewal in ${daysToRenewal}d`,
    urgent: daysToRenewal <= RENEWAL_URGENT_DAYS,
  };
}

// One-line status summary for a card: counts that need attention, or the
// card's degraded/inactive state, plus the staleness stamp for parked cards.
export function clientCountsSummary(c: ClientCockpitCard): string {
  if (c.error) return "status unavailable";
  if (!c.has_state) return "not yet activated";
  const counts =
    [
      c.overdue_actions ? `${c.overdue_actions} overdue` : null,
      c.awaiting_replies ? `${c.awaiting_replies} awaiting reply` : null,
      c.unread_alerts ? `${c.unread_alerts} alerts` : null,
    ]
      .filter(Boolean)
      .join(" · ") || "all quiet";
  const stamp =
    !c.is_active && c.saved_at
      ? ` · as of ${new Date(c.saved_at).toLocaleDateString()}`
      : "";
  return counts + stamp;
}
