"use client";

import Button from "@/components/ui/Button";

// The chat: steps the Executive held back after reading something someone
// else wrote (an email, a file, a web page), shown under its reply while they
// wait on your OK (orchestrator/outside_content.py `pending_held`). Approve
// sends "Go ahead" as your next message, the same go as typing it; Not now
// just hides the card, and the steps stay undone.

export const APPROVE_MESSAGE = "Go ahead";

export default function HeldStepsCard({
  steps,
  onApprove,
  onDismiss,
  disabled = false,
}: {
  steps: string[];
  onApprove: () => void;
  onDismiss: () => void;
  disabled?: boolean;
}) {
  if (steps.length === 0) return null;
  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Needs your OK</div>
      <ul className="mt-1 space-y-0.5">
        {steps.map((step) => (
          <li key={step} className="text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
            {step}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-sm text-fg-muted">
        I read something from outside your team, so I&apos;m checking with you first.
      </p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={onApprove} disabled={disabled}>
          Approve
        </Button>
        <Button variant="ghost" className="ml-auto" onClick={onDismiss} disabled={disabled}>
          Not now
        </Button>
      </div>
    </article>
  );
}
