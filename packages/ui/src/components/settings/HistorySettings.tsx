"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import { getHistory, updateHistorySettings, type HistoryState } from "@/lib/api";
import { personRetentionChoices, retentionLabel } from "@/lib/history";

// Always in the loop's settings (PUT /memories/history/settings): each
// person's own "Keep track of what happens" switch and how long their notes
// last, on Act as me; the company-wide retention, the owner's, on Memory.

const SELECT =
  "w-full sm:w-auto bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50";

function useHistoryState(): {
  state: HistoryState | null | "loading" | "error";
  save: (patch: Parameters<typeof updateHistorySettings>[0]) => Promise<void>;
  busy: boolean;
  error: string | null;
} {
  const [state, setState] = useState<HistoryState | null | "loading" | "error">("loading");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    getHistory(undefined, ctrl.signal)
      .then(setState)
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => ctrl.abort();
  }, []);

  const save = async (patch: Parameters<typeof updateHistorySettings>[0]) => {
    setBusy(true);
    setError(null);
    try {
      setState(await updateHistorySettings(patch));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save the setting.");
    } finally {
      setBusy(false);
    }
  };

  return { state, save, busy, error };
}

const asValue = (days: number | null) => (days === null ? "" : String(days));
const fromValue = (value: string) => (value === "" ? null : Number(value));

/** Act as me → "Keep track of what happens": the person's own switch, and a
 * shorter time for their own notes. Nothing for someone who can't have it. */
export function KeepTrackCard() {
  const { state, save, busy, error } = useHistoryState();
  if (state === "loading" || state === "error" || state === null) return null;
  // Someone who can't use Act as me can still turn it off.
  if (!state.can_note_replies && !state.reply_notes) return null;

  const on = state.reply_notes;
  const company = state.company_retention_days;
  return (
    <SettingsCard
      title="Keep track of what happens"
      titleId="keep-track-label"
      description={
        on
          ? "On: after you send a reply it drafted, it keeps private notes of what you said, so it can remind you later. Only you see them."
          : "Off: nothing from your replies is noted."
      }
      action={
        <Switch
          checked={on}
          onChange={() => void save({ reply_notes: !on })}
          disabled={busy || (!on && !state.can_note_replies)}
          labelledBy="keep-track-label"
        />
      }
    >
      {on && (
        <div className="space-y-3">
          <label className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3">
            <span className="text-sm text-fg-muted">Keep my notes for</span>
            <select
              className={SELECT}
              value={asValue(state.retention_days)}
              disabled={busy}
              onChange={(e) => void save({ retention_days: fromValue(e.target.value) })}
            >
              {personRetentionChoices(state.retention_choices, company, state.retention_days).map((days) => (
                <option key={asValue(days)} value={asValue(days)}>
                  {days === null ? `The company default (${retentionLabel(company)})` : retentionLabel(days)}
                </option>
              ))}
            </select>
          </label>
          <Link href="/memories?tab=history" className="inline-block text-sm font-medium text-accent hover:underline">
            See your notes
          </Link>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

/** Memory → how long notes last for everyone: the owner picks, everyone else
 * reads it. */
export function CompanyRetentionCard() {
  const { state, save, busy, error } = useHistoryState();
  if (state === "loading") return <p className="text-[15px] text-fg-muted">Loading…</p>;
  if (state === "error" || state === null) {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted">
          {state === null ? "Sign in to see how long your notes last." : "Couldn't load this setting."}
        </p>
      </SettingsCard>
    );
  }

  const company = state.company_retention_days;
  return (
    <SettingsCard
      title="How long notes last"
      description="Notes from replies people approve and send are forgotten after this, unless they pin them. Each person can choose a shorter time for their own notes in Act as me."
    >
      {state.can_set_company_retention ? (
        <label className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3">
          <span className="text-sm text-fg-muted">For everyone</span>
          <select
            className={SELECT}
            value={asValue(company)}
            disabled={busy}
            onChange={(e) => void save({ company_retention_days: fromValue(e.target.value) })}
          >
            {state.retention_choices.map((days) => (
              <option key={asValue(days)} value={asValue(days)}>
                {days === null ? "Until they're forgotten" : retentionLabel(days)}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p className="text-[15px] text-fg">
          {company === null ? "Notes are kept until they're forgotten." : `Notes last ${retentionLabel(company)}.`}{" "}
          <span className="text-fg-muted">The owner of this Open Executive sets this.</span>
        </p>
      )}
      <p className="mt-3 text-sm text-fg-muted">
        {state.effective_retention_days === company
          ? ""
          : `Your own notes last ${retentionLabel(state.effective_retention_days)}. `}
        <Link href="/memories?tab=history" className="font-medium text-accent hover:underline">
          See your notes
        </Link>
      </p>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
