"use client";

// A send or edit a team member asked for in chat that their department held
// (orchestrator.department_gate). Approve carries out exactly the call shown,
// on the server; it stays off until the whole call has loaded, since the
// card's headline keeps only the start of it.

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  approveDecision,
  getHeldAction,
  rejectDecision,
  type HeldActionPayload,
  type ProposalItem,
} from "@/lib/api";

import { Chip } from "./shared";

// The tag such a card carries (department_gate.DECISION_CLASS).
export const HELD_ACTION_TAG = "decision_class:department_action";

export function isHeldAction(proposal: ProposalItem): boolean {
  return proposal.decision_instance_id != null && (proposal.topic_tags ?? []).includes(HELD_ACTION_TAG);
}

export default function HeldActionCard({
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
  const [held, setHeld] = useState<HeldActionPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (decisionId == null) return;
    const controller = new AbortController();
    getHeldAction(decisionId, controller.signal)
      .then(setHeld)
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setError("Couldn't load what it would do.");
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

  return (
    <div
      id={`alert-${proposal.alert_id}`}
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
          {proposal.headline}
        </div>
        <span className="flex-shrink-0">
          <Chip tone="amber">Waiting on you</Chip>
        </span>
      </div>
      {held?.reason && (
        <p className="mt-1.5 text-[15px] leading-relaxed text-fg-muted">It waited because {held.reason}.</p>
      )}
      {held && held.details.length > 0 && (
        <dl className="mt-3 flex flex-col gap-2 rounded-xl border border-line bg-surface p-3">
          {held.details.map((d) => (
            <div key={d.field} className="min-w-0">
              <dt className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{d.field}</dt>
              <dd className="whitespace-pre-wrap break-words text-[15px] leading-snug text-fg">{d.value}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="mt-3 grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
        <Button
          variant="primary"
          disabled={busy || held == null}
          onClick={() => void run(() => approveDecision(decisionId))}
        >
          Approve
        </Button>
        <Button variant="ghost" disabled={busy} onClick={() => void run(() => rejectDecision(decisionId))}>
          Decline
        </Button>
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </div>
  );
}
