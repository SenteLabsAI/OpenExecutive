"use client";

import { useCallback, useEffect, useState } from "react";

import { useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";
import SpendingMeter from "@/components/spending/SpendingMeter";
import { getSpending, updateWorkspace } from "@/lib/api";
import { parseLimit, type SpendingSummary } from "@/lib/spending";

// Settings → Monthly AI limit: this month's estimated spending, and the
// amount at which background work pauses (PUT /workspace
// `monthly_budget_usd`). The API applies a change at once — pausing or
// lifting — so the Executive's status is re-read after a save.
export default function SpendingCard() {
  const { refresh: refreshStatus } = useExecutiveStatus();
  const [spending, setSpending] = useState<SpendingSummary | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await getSpending(signal);
      setSpending(next);
      setDraft(next.limit_usd === null ? "" : String(next.limit_usd));
      setLoadFailed(false);
    } catch {
      if (!signal?.aborted) setLoadFailed(true);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function save(value: number | null) {
    setBusy(true);
    setError(null);
    try {
      await updateWorkspace({ monthly_budget_usd: value });
      await load();
      refreshStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the limit.");
    } finally {
      setBusy(false);
    }
  }

  const parsed = parseLimit(draft);
  const unchanged = parsed.ok && parsed.value === (spending?.limit_usd ?? null);

  return (
    <section id="monthly-limit" className="rounded-xl border border-line bg-surface-elevated p-4 scroll-mt-4">
      <h2 className="text-sm font-medium text-fg">Monthly AI limit</h2>
      <p className="mt-1 text-xs text-fg-muted leading-relaxed">
        When this month&apos;s AI spending reaches the limit, background work — briefs, research,
        alert reviews and email checks — pauses until the 1st of next month. Chat keeps working.
        Spending is estimated from list prices; your AI provider&apos;s bill is the final word.
      </p>

      {loadFailed && !spending && (
        <p className="mt-3 text-xs text-red-400">Couldn&apos;t load this month&apos;s spending.</p>
      )}

      {spending && (
        <div className="mt-4 max-w-md space-y-3">
          <SpendingMeter spending={spending} />
          {spending.can_change_limit ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (parsed.ok && !unchanged) void save(parsed.value);
              }}
              className="space-y-1.5"
            >
              <label htmlFor="monthly-limit-usd" className="block text-xs font-medium text-fg">
                Limit per month
              </label>
              <div className="flex items-center gap-2">
                <div className="flex-1 flex items-center rounded-lg border border-line bg-surface focus-within:border-line-strong">
                  <span className="pl-2.5 text-sm text-fg-muted" aria-hidden="true">
                    $
                  </span>
                  <input
                    id="monthly-limit-usd"
                    type="text"
                    inputMode="decimal"
                    value={draft}
                    disabled={busy}
                    onChange={(e) => {
                      setError(null);
                      setDraft(e.target.value);
                    }}
                    placeholder="No limit"
                    className="w-full px-1.5 py-1.5 bg-transparent text-sm text-fg placeholder:text-fg-subtle focus:outline-none disabled:opacity-60"
                  />
                </div>
                <button
                  type="submit"
                  disabled={busy || !parsed.ok || unchanged}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-500 hover:bg-indigo-600 text-white transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {busy ? "Saving…" : "Save"}
                </button>
                {spending.limit_usd !== null && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void save(null)}
                    className="px-3 py-1.5 rounded-lg text-xs text-fg-muted hover:text-fg transition-colors cursor-pointer disabled:opacity-50"
                  >
                    Remove limit
                  </button>
                )}
              </div>
              {!parsed.ok && <p className="text-xs text-amber-400">{parsed.error}</p>}
            </form>
          ) : (
            <p className="text-xs text-fg-muted">Only the owner can change the limit.</p>
          )}
          {error && <p className="text-xs text-red-400">{error}</p>}
        </div>
      )}
    </section>
  );
}
