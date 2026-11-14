"use client";

// A reply box on a card that waits for an OK: the person says what to do
// instead ("don't ask him, share the whole folder"), the Executive rewrites
// the card's "If you approve" plan in a few seconds, and Approve then does
// exactly the new plan (POST /alerts/{id}/plan). Undo change sits beside
// Approve on the card.

import { useState } from "react";

import { type PlanResult, changeCardPlan } from "@/lib/api";
import { isImeComposing } from "@/lib/imeComposing";

const FIELD =
  "min-w-0 flex-1 resize-none [field-sizing:content] max-h-40 bg-transparent px-1 py-1 text-[15px] leading-snug text-fg placeholder:text-fg-subtle focus:outline-none disabled:opacity-50";

export default function CardPlanReply({
  alertId,
  changed,
  disabled = false,
  onChanged,
}: {
  alertId: number;
  // The plan on the card is already one the person changed.
  changed: boolean;
  disabled?: boolean;
  onChanged: (result: PlanResult) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fieldId = `card-plan-${alertId}`;

  const send = async () => {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await changeCardPlan(alertId, value);
      setText("");
      setSaid(r.reply);
      onChanged(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't send that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="mt-3"
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <label htmlFor={fieldId} className="sr-only">
        Tell me what to do instead
      </label>
      <div className="flex items-end gap-2 rounded-xl border border-line bg-surface px-2.5 py-2 focus-within:ring-2 focus-within:ring-accent/40">
        <textarea
          id={fieldId}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !isImeComposing(e)) {
              e.preventDefault();
              void send();
            }
          }}
          rows={1}
          maxLength={500}
          disabled={busy || disabled}
          placeholder={changed ? "Change it again" : "Tell me what to do instead"}
          className={FIELD}
        />
        <button
          type="submit"
          disabled={busy || disabled || !text.trim()}
          aria-label="Update the plan"
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-accent text-white disabled:opacity-40"
        >
          {busy ? (
            <span aria-hidden="true" className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
          ) : (
            <span aria-hidden="true">↑</span>
          )}
        </button>
      </div>
      {error ? (
        <p role="alert" className="mt-1 text-sm text-red-500">
          {error}
        </p>
      ) : (
        <p className="mt-1 text-xs text-fg-subtle" aria-live="polite">
          {busy ? "Updating the plan…" : (said ?? "Tell me what to do instead. I'll update the plan.")}
        </p>
      )}
    </form>
  );
}
