"use client";

import { useEffect, useState } from "react";

import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import {
  allowLeadSuggestion,
  getTakeTheLead,
  removeLeadLearned,
  setTakeTheLead,
  type OutsideContentMode,
  type TakeTheLead,
} from "@/lib/api";
import { APPROVALS_LEADS, APPROVALS_SENDS } from "@/lib/reach";
import { PageSpinner } from "@/components/ui/Spinner";

// Delegate → As me (Take the lead, GET/PUT /take-the-lead): what the
// Executive does in its own name without being asked. It has no switch of
// its own: Pause stops it with everything else, and Approvals decides each
// step. In training sits above Approvals: everything it would do comes to
// you as a card on Home, with "Do this on its own next time"; what you allow
// is on the What it's learned list, with Remove. The owner's alone: anyone
// else sees who can change it.

export function useTakeTheLead(): {
  lead: TakeTheLead | null;
  state: "loading" | "hidden" | "ready" | "error";
  setLead: (next: TakeTheLead) => void;
} {
  const [lead, setLead] = useState<TakeTheLead | null>(null);
  const [state, setState] = useState<"loading" | "hidden" | "ready" | "error">("loading");

  useEffect(() => {
    const controller = new AbortController();
    getTakeTheLead(controller.signal)
      .then((next) => {
        setLead(next);
        setState(next ? "ready" : "hidden");
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => controller.abort();
  }, []);

  return { lead, state, setLead };
}

export default function AsMeCard({
  lead,
  state,
  setLead,
  level,
}: {
  lead: TakeTheLead | null;
  state: "loading" | "hidden" | "ready" | "error";
  setLead: (next: TakeTheLead) => void;
  level: OutsideContentMode | null;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (change: () => Promise<TakeTheLead>) => {
    setBusy(true);
    setError(null);
    try {
      setLead(await change());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  // What it was allowed in its own name. The same table holds As you's
  // allowances and writing examples (feature act_as_me), listed on As you.
  const learned = lead ? lead.learned.filter((item) => item.feature !== "act_as_me") : [];

  const levelLine = level ? `Approvals is on ${APPROVALS_SENDS[level].label}. ${APPROVALS_LEADS[level]}` : "Approvals decides what waits for your yes.";

  return (
    <>
      <SettingsCard
        title="On or paused"
        description="On, it acts on what it finds. Paused, it only answers when asked. Briefs and nudges stop too."
      >
        <ExecutiveRunSwitch />
        <p className="mt-4 border-t border-line pt-4 text-[15px] text-fg">
          <span className="text-fg-muted">Now: </span>
          {lead?.training ? "In training: everything it would do comes to you first, until you allow it." : levelLine}
        </p>
      </SettingsCard>

      {state === "loading" && <PageSpinner />}
      {state === "hidden" && (
        <SettingsCard title="In training">
          <p className="text-sm text-fg-muted">
            Only the account owner can put the Executive in training or allow what it does on its own.
          </p>
        </SettingsCard>
      )}
      {state === "error" && (
        <SettingsCard>
          <p className="text-sm text-fg-muted">Couldn&apos;t load In training.</p>
        </SettingsCard>
      )}
      {state === "ready" && lead && (
        <SettingsCard
          title="In training"
          titleId="as-me-training-label"
          description={
            lead.training
              ? "Everything it would do comes to you first as a card on Home, whatever Approvals says. Tick “Do this on its own next time” when you approve one."
              : "Off: Approvals alone decides what waits for your yes."
          }
          action={
            <Switch
              checked={lead.training}
              onChange={(next) => void run(() => setTakeTheLead({ training: next }))}
              disabled={busy}
              labelledBy="as-me-training-label"
            />
          }
        >
          {(lead.training || learned.length > 0 || lead.suggested.length > 0) && (
            <div>
              <h3 className="text-[15px] font-semibold text-fg">What it&apos;s learned</h3>
              <p className="mt-1 text-sm text-fg-muted">
                These go on their own, where Approvals lets them. Remove one and it asks again.
              </p>
              {learned.length === 0 && lead.suggested.length === 0 ? (
                <p className="mt-3 text-sm text-fg-muted">Nothing yet.</p>
              ) : (
                <ul className="mt-3 flex flex-col divide-y divide-line rounded-xl border border-line">
                  {learned.map((item) => {
                    const example = Object.values(item.example).join(" · ");
                    return (
                      <li key={item.id} className="flex min-h-touch items-center justify-between gap-3 px-4 py-2">
                        <span className="min-w-0">
                          <span className="block text-[15px] font-medium">{item.label}</span>
                          <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted line-clamp-2">
                            Allowed {new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                            {item.uses > 0 ? ` · done ${item.uses} ${item.uses === 1 ? "time" : "times"} since` : ""}
                            {example ? ` · like “${example}”` : ""}
                          </span>
                        </span>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void run(() => removeLeadLearned(item.id))}
                          className="flex-shrink-0 text-sm font-semibold text-accent hover:underline disabled:opacity-60"
                          aria-label={`Remove ${item.label}`}
                        >
                          Remove
                        </button>
                      </li>
                    );
                  })}
                  {lead.suggested.map((item) => (
                    <li key={item.key} className="flex min-h-touch items-center justify-between gap-3 px-4 py-2">
                      <span className="min-w-0">
                        <span className="block text-[15px] font-medium">{item.label}</span>
                        <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted">
                          Suggested: you approved it {item.approvals} times unchanged
                        </span>
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void run(() => allowLeadSuggestion(item.key))}
                        className="flex-shrink-0 text-sm font-semibold text-accent hover:underline disabled:opacity-60"
                        aria-label={`Allow ${item.label}`}
                      >
                        Allow
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
        </SettingsCard>
      )}
    </>
  );
}
