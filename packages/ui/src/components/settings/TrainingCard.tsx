"use client";

import { useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import {
  removeTrainingLearned,
  type DelegationSettings,
  type Training,
  type TrainingLearned,
} from "@/lib/api";

// What Learn from my edits kept (delegation/training.py): how you write to
// each person, from drafts you changed before sending, with Remove
// (DELETE /delegation/learned/{id}). Shown on Delegate → Act as me while
// Learn from my edits is on or something is on the list.

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

function learnedLine(item: TrainingLearned): string {
  const when = new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const parts = [`learned ${when}`];
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
  const learned = training.learned.filter((item) => item.setting === "drafts");
  if (!training.drafts && learned.length === 0) return null;
  const titleId = "learned-label";
  return (
    <SettingsCard
      title="What it's learned"
      titleId={titleId}
      description="How you write to each person, from your edits. Yours alone."
    >
      {learned.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing yet.</p>
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
