"use client";

import { useCallback, useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import FeatureName from "@/components/FeatureName";
import {
  approveActionCard,
  dismissActionCard,
  getActionCards,
  type ActionCard,
  type ActionCardResult,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";

// Home: actions the Executive suggested from your email (Act as me) — a
// meeting, a message to someone, a new contact — shown as cards in "Needs
// you". Each action is spelled out exactly as it will happen, with a box to
// leave it out. Approve does the ticked ones; Dismiss drops the card. GET
// /delegation/actions answers only the card's own person, so nothing shows
// for anyone else, on a backend without it, and when nothing is waiting.

export function useActionCards() {
  const [cards, setCards] = useState<ActionCard[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getActionCards(controller.signal)
      .then(setCards)
      .catch(() => { /* no card is better than a broken one */ });
    return () => controller.abort();
  }, []);

  const gone = useCallback((decisionId: number) => {
    setCards((prev) => (prev ?? []).filter((c) => c.decision_id !== decisionId));
  }, []);

  return { cards: cards ?? [], gone };
}

// What the action cards are, for the Needs you header's tip.
export const ACTION_CARDS_TIP =
  "Things your email called for that the Executive can't do on its own after reading it: " +
  "a meeting, a message to someone, a new contact. Each is shown exactly as it will happen, " +
  "and nothing happens until you tap Approve. Untick anything you don't want. Only you see these.";

const KIND_LABEL: Record<string, string> = {
  invite: "Meeting",
  message: "Message",
  add_contact: "New contact",
};

const RESULT_LABEL: Record<string, string> = {
  done: "Done",
  waiting: "Waiting for approval",
  failed: "Didn't happen",
  skipped: "Left out",
};

function resultTone(status: string): string {
  if (status === "done") return "text-emerald-700 dark:text-emerald-300";
  if (status === "failed") return "text-red-500";
  return "text-fg-muted";
}

export function ActionCardItem({
  card,
  onGone,
  emphasized = false,
}: {
  card: ActionCard;
  emphasized?: boolean;
  onGone: (id: number) => void;
}) {
  const [ticked, setTicked] = useState<Set<number>>(() => new Set(card.actions.map((a) => a.index)));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<ActionCardResult[] | null>(null);
  const created = formatRelativeTime(card.created_at);

  const toggle = (index: number) =>
    setTicked((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  const approve = async () => {
    setBusy("Doing it…");
    setError(null);
    try {
      setResults(await approveActionCard(card.decision_id, [...ticked].sort((a, b) => a - b)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't do that.");
    } finally {
      setBusy(null);
    }
  };

  const dismiss = async () => {
    setBusy("Dismissing…");
    setError(null);
    try {
      await dismissActionCard(card.decision_id);
      onGone(card.decision_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't dismiss that card.");
      setBusy(null);
    }
  };

  const resultFor = (index: number) => results?.find((r) => r.index === index);
  return (
    <article
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
          To approve
        </span>
        <FeatureName feature="act_as_me" className="text-[12px]" />
        {created && <span className="text-sm text-fg-subtle tabular-nums">{created}</span>}
      </div>
      {card.why && (
        <div className="text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">{card.why}</div>
      )}

      <ul className="mt-3 space-y-2">
        {card.actions.map((action) => {
          const result = resultFor(action.index);
          return (
            <li key={action.index} className="rounded-xl border border-line bg-surface px-3 py-2">
              <label className="flex items-start gap-3">
                {!results && (
                  <input
                    type="checkbox"
                    className="mt-1 h-4 w-4 shrink-0 accent-[rgb(var(--accent-strong))]"
                    checked={ticked.has(action.index)}
                    onChange={() => toggle(action.index)}
                    disabled={busy !== null}
                    aria-label={`Include: ${action.summary}`}
                  />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block text-xs font-semibold uppercase tracking-wide text-fg-muted">
                    {KIND_LABEL[action.kind] ?? action.kind}
                  </span>
                  <span className="block text-sm text-fg break-words">{action.summary}</span>
                  {/* Plain text: shown exactly as it will be sent, never as markup. */}
                  {action.text && (
                    <span className="mt-1 block max-h-40 overflow-y-auto whitespace-pre-wrap break-words text-sm text-fg-muted">
                      {action.text}
                    </span>
                  )}
                  {result && (
                    <span className={`mt-1 block text-sm ${resultTone(result.status)}`}>
                      {RESULT_LABEL[result.status] ?? result.status}
                      {result.status !== "done" && result.status !== "skipped" && result.detail ? `: ${result.detail}` : ""}
                    </span>
                  )}
                </span>
              </label>
            </li>
          );
        })}
      </ul>

      {results ? (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="secondary" onClick={() => onGone(card.decision_id)}>
            Close
          </Button>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => void approve()} disabled={busy !== null || ticked.size === 0}>
            {busy ?? (ticked.size === card.actions.length ? "Approve" : `Approve ${ticked.size}`)}
          </Button>
          <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy !== null}>
            Dismiss
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}

// The chat: the cards a reply left (its propose_actions chips carry their
// ids), shown under it while they wait. A card already approved, dismissed
// or expired shows nothing here; its chip still links to Today.
export function ChatActionCards({ ids }: { ids: number[] }) {
  const { cards, gone } = useActionCards();
  const shown = cards.filter((c) => ids.includes(c.decision_id));
  if (shown.length === 0) return null;
  return (
    <div className="mt-3 space-y-3">
      {shown.map((card) => (
        <ActionCardItem key={card.decision_id} card={card} onGone={gone} />
      ))}
    </div>
  );
}
