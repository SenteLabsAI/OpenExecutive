"use client";

import { useState } from "react";

import ModePicker from "@/components/settings/ModePicker";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  removeTrainingLearned,
  setTraining,
  type DelegationSettings,
  type Training,
  type TrainingLearned,
  type TrainingSetting,
} from "@/lib/api";

// Training on Settings → Act as me (delegation/training.py), the way Take the
// lead has it: like someone new, each job can be In training, where it brings
// you its work first and learns who and how from what you approve. Each job's
// card carries its own Off / In training / On (Write drafts as me, Handle it
// for me); this file holds the note at the top, the Suggested actions card
// (Always ask / In training, PUT /delegation/training) and, at the bottom, one
// "What it's learned" list, each item named by its job, with Remove
// (DELETE /delegation/learned/{id}).

// The job each learned item came from, as its card is titled.
const JOB_NAMES: Record<TrainingSetting, string> = {
  replies: "Handle it for me, replies",
  follow_ups: "Handle it for me, follow-ups",
  actions: "Suggested actions",
  drafts: "Write drafts as me",
};

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

export function TrainingNote() {
  return (
    <section className="rounded-2xl border border-accent/30 bg-accent/5 p-5 sm:p-6">
      <h2 className="text-base sm:text-lg font-semibold text-fg">Start it in training</h2>
      <p className="mt-1 text-sm text-fg leading-relaxed">
        Like anyone you hire, it can start each job in training: it brings you its work first and learns from what you
        approve. When you trust it with a job, move that job to On.
      </p>
    </section>
  );
}

type ActionsMode = "ask" | "training";

export function SuggestedActionsCard({
  training,
  onSettings,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, run } = useSave(onSettings);
  const mode: ActionsMode = training.actions ? "training" : "ask";
  return (
    <SettingsCard
      title="Suggested actions"
      titleId="act-as-me-actions-label"
      description="Meeting invites and messages your mail calls for."
    >
      <ModePicker<ActionsMode>
        labelledBy="act-as-me-actions-label"
        value={mode}
        disabled={busy}
        options={[
          { value: "ask", label: "Always ask" },
          { value: "training", label: "In training" },
        ]}
        onPick={(next) => void run(() => setTraining({ actions: next === "training" }), "Could not change training.")}
      />
      <p className="mt-3 text-sm text-fg-muted leading-relaxed">
        {mode === "training"
          ? "Each one comes to you as a card. Tap Approve + allow and it does that kind, with those people, on its own next time."
          : "Each one comes to you as a card, every time."}
      </p>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

function times(n: number): string {
  return n === 1 ? "once" : n === 2 ? "twice" : `${n} times`;
}

function learnedLine(item: TrainingLearned): string {
  const when = new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const parts = [JOB_NAMES[item.setting] ?? "Act as me"];
  parts.push(item.setting === "drafts" ? `learned ${when}` : `allowed ${when}`);
  if (item.uses > 0) parts.push(`done ${times(item.uses)} since`);
  if (item.example) parts.push(`writes like “${item.example}”`);
  return parts.join(" · ");
}

// Shown while any job is in training or something is on the list.
export function LearnedCard({
  training,
  onSettings,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, run } = useSave(onSettings);
  const learned = training.learned;
  const anyOn = training.replies || training.follow_ups || training.actions || training.drafts;
  if (!anyOn && learned.length === 0) return null;
  return (
    <SettingsCard
      title="What it's learned"
      titleId="act-as-me-learned-label"
      description="From its training. Yours alone. Remove one and it asks you again."
    >
      {learned.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing yet.</p>
      ) : (
        <ul
          aria-labelledby="act-as-me-learned-label"
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
