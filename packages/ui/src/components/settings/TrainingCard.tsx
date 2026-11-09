"use client";

import { useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import {
  removeTrainingLearned,
  type DelegationSettings,
  type Training,
  type TrainingLearned,
} from "@/lib/api";

// What In training kept for As you (delegation/training.py): who it may
// send to on its own ("training", from "Send to them on its own next time"
// on a card) and how you write to each person ("drafts", from Learn from my
// edits), with Remove (DELETE /delegation/learned/{id}). Yours alone. Shown
// on Delegate → As you while either is on or something is on the list.

function useSave(onSettings: (next: DelegationSettings) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (work: () => Promise<DelegationSettings>, fallback: string) => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await work());
    } catch (err) {
      setError(err instanceof Error ? err.message : fallback);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

function times(n: number): string {
  return n === 1 ? "once" : n === 2 ? "twice" : `${n} times`;
}

export function learnedLine(item: TrainingLearned): string {
  const when = new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const parts = [item.setting === "training" ? `allowed ${when}` : `learned ${when}`];
  if (item.uses > 0) parts.push(`done ${times(item.uses)} since`);
  if (item.example) parts.push(`writes like “${item.example}”`);
  return parts.join(" · ");
}

export function LearnedCard({
  training,
  onSettings,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, run } = useSave(onSettings);
  const learned = training.learned.filter((item) => item.setting === "training" || item.setting === "drafts");
  if (!training.training && !training.drafts && learned.length === 0) return null;
  const titleId = "learned-label";
  return (
    <SettingsCard
      title="What it's learned as you"
      titleId={titleId}
      description="Who it may send to on its own, and how you write to each person. Remove one and it asks again."
    >
      {learned.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing yet. Tick “on its own next time” or “Do it like this next time” on a card.</p>
      ) : (
        <ul
          aria-labelledby={titleId}
          className="flex flex-col divide-y divide-line rounded-xl border border-line"
        >
          {learned.map((item) => (
            <li key={item.id} className="flex min-h-touch items-center justify-between gap-3 px-4 py-2">
              <span className="min-w-0">
                <span className="block text-[15px] font-medium break-words">{item.label}</span>
                <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted line-clamp-2">
                  {learnedLine(item)}
                </span>
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(() => removeTrainingLearned(item.id), "Could not remove that.")}
                className="flex-shrink-0 min-h-touch text-sm font-semibold text-accent hover:underline disabled:opacity-60"
                aria-label={`Remove ${item.label}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
