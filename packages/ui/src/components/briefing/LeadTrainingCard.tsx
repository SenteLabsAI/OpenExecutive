"use client";

// A Take the lead card from training: something the Executive wanted to do on
// its own that you haven't allowed yet. Approve does it once; Approve + allow
// also lets it do this (the same action with the same person) from now on;
// Edit changes it first, and with "Do it like this next time" ticked keeps
// the edit as an example and allows it. Answered here, never through chat.

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  approveLeadCard,
  getLeadCard,
  rejectDecision,
  type LeadCardPayload,
  type ProposalItem,
} from "@/lib/api";

import { Chip } from "./shared";

// The tag a training card carries (orchestrator.take_the_lead.TRAINING_TAG).
export const LEAD_TRAINING_TAG = "take_the_lead:training";

export function isLeadTraining(proposal: ProposalItem): boolean {
  return proposal.decision_instance_id != null && (proposal.topic_tags ?? []).includes(LEAD_TRAINING_TAG);
}

export default function LeadTrainingCard({
  proposal,
  onResolved,
  emphasized = false,
}: {
  proposal: ProposalItem;
  // Called once it's approved or declined, so the briefing can drop the card.
  onResolved: () => void;
  emphasized?: boolean;
}) {
  const decisionId = proposal.decision_instance_id;
  const [card, setCard] = useState<LeadCardPayload | null>(null);
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [learn, setLearn] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (decisionId == null) return;
    const controller = new AbortController();
    getLeadCard(decisionId, controller.signal)
      .then((next) => {
        setCard(next);
        setValues(Object.fromEntries(next.fields.map((f) => [f.field, f.value])));
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setError("Couldn't load the details of this card.");
      });
    return () => controller.abort();
  }, [decisionId]);

  if (decisionId == null) return null;

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onResolved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  }

  const allowLabel = card?.allow?.label ?? null;
  const headline = card?.summary || proposal.headline;
  const changed = Object.fromEntries(
    (card?.fields ?? []).filter((f) => (values[f.field] ?? "") !== f.value).map((f) => [f.field, values[f.field] ?? ""]),
  );

  return (
    <div
      id={`alert-${proposal.alert_id}`}
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
          {headline}
        </div>
        <span className="flex-shrink-0">
          <Chip tone="amber">Training</Chip>
        </span>
      </div>
      <p className="mt-1.5 text-[15px] leading-relaxed text-fg-muted">
        It&apos;s in training, so it asks before doing anything you haven&apos;t allowed yet.
      </p>

      {editing ? (
        <div className="mt-3 flex flex-col gap-3">
          {(card?.fields ?? []).map((f) => {
            const id = `lead-${decisionId}-${f.field}`;
            const common =
              "w-full rounded-xl border border-line bg-surface p-3 text-[15px] leading-snug text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50";
            return (
              <label key={f.field} htmlFor={id} className="flex flex-col gap-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{f.label}</span>
                {f.long ? (
                  <textarea
                    id={id}
                    rows={5}
                    value={values[f.field] ?? ""}
                    disabled={busy}
                    onChange={(e) => setValues({ ...values, [f.field]: e.target.value })}
                    className={`${common} whitespace-pre-wrap`}
                  />
                ) : (
                  <input
                    id={id}
                    value={values[f.field] ?? ""}
                    disabled={busy}
                    onChange={(e) => setValues({ ...values, [f.field]: e.target.value })}
                    className={common}
                  />
                )}
              </label>
            );
          })}
          {allowLabel && (
            <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-accent/30 bg-accent/5 p-3">
              <input
                type="checkbox"
                checked={learn}
                disabled={busy}
                onChange={(e) => setLearn(e.target.checked)}
                className="mt-1 h-4 w-4 flex-shrink-0 accent-[rgb(var(--accent-strong))]"
              />
              <span>
                <span className="block text-[15px] font-semibold text-fg">Do it like this next time</span>
                <span className="mt-0.5 block text-sm leading-snug text-fg-muted">
                  It learns from your edit and does this on its own from now on: {allowLabel}.
                </span>
              </span>
            </label>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={() => setEditing(false)} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={busy}
              onClick={() =>
                void run(() =>
                  approveLeadCard(decisionId, {
                    input: Object.keys(changed).length > 0 ? changed : undefined,
                    allow: Boolean(allowLabel && learn),
                  }),
                )
              }
            >
              Approve
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="mt-3 grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
            <Button variant="primary" disabled={busy} onClick={() => void run(() => approveLeadCard(decisionId, {}))}>
              Approve
            </Button>
            {allowLabel && (
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => void run(() => approveLeadCard(decisionId, { allow: true }))}
              >
                Approve + allow
              </Button>
            )}
            {(card?.fields.length ?? 0) > 0 && (
              <Button variant="secondary" disabled={busy} onClick={() => setEditing(true)}>
                Edit
              </Button>
            )}
            <Button variant="ghost" disabled={busy} onClick={() => void run(() => rejectDecision(decisionId))}>
              Decline
            </Button>
          </div>
          {allowLabel && (
            <p className="mt-2 text-[13px] leading-snug text-fg-muted">
              Approve + allow: from now on it does this without asking: {allowLabel}.
            </p>
          )}
        </>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </div>
  );
}
