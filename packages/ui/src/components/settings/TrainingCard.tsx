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

// Training on Delegate (delegation/training.py), the way Take the lead has
// it: like someone new, each job can be In training, where it brings you its
// work first and learns who and how from what you approve. Replies and
// follow-ups are trained from What it sends on its own (SendsCard, Handle it
// for me), drafts from What it writes as you (WritesCard, Act as me). This
// file holds the Suggested actions card (Always ask / In training / On,
// PUT /delegation/training) and the learned list, shown on each tab for its
// own jobs, each item named by its job, with Remove
// (DELETE /delegation/learned/{id}).

// The job each learned item came from, as its card is titled.
const JOB_NAMES: Record<TrainingSetting, string> = {
  replies: "Replies",
  follow_ups: "Follow-ups",
  actions: "Suggested actions",
  drafts: "Drafts",
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

type ActionsMode = "ask" | "training" | "on";

const ACTIONS_LINES: Record<ActionsMode, string> = {
  ask: "Each one comes to you as a card, every time.",
  training:
    "Each one comes to you as a card. Tick “on its own next time” and it does that kind, with those people, on its own from then on.",
  on: "It sends the message or the invite on its own, then shows you what it did.",
};

export function SuggestedActionsCard({
  training,
  onSettings,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, run } = useSave(onSettings);
  const mode: ActionsMode = training.actions ? "training" : training.actions_on ? "on" : "ask";
  const pick = (next: ActionsMode) =>
    void run(
      () =>
        setTraining(
          next === "on"
            ? { actions_on: true }
            : next === "training"
              ? { actions: true }
              : { actions: false, actions_on: false },
        ),
      "Could not change Suggested actions.",
    );
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
          { value: "on", label: "On" },
        ]}
        onPick={pick}
      />
      <p className="mt-3 text-sm text-fg-muted leading-relaxed">{ACTIONS_LINES[mode]}</p>
      {mode === "on" && (
        <p className="mt-2 text-sm text-fg-muted leading-relaxed">
          New contacts, anyone added in the last week, money, links and the topics that always wait still come to
          you. Up to 10 a day.
        </p>
      )}
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

// What it learned for `jobs`, shown while one of them is in training or
// something of theirs is on the list.
export function LearnedCard({
  training,
  onSettings,
  jobs,
  title,
  description,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
  jobs: TrainingSetting[];
  title: string;
  description: string;
}) {
  const { busy, error, run } = useSave(onSettings);
  const learned = training.learned.filter((item) => jobs.includes(item.setting));
  const anyOn = jobs.some((job) => Boolean(training[job]));
  if (!anyOn && learned.length === 0) return null;
  const titleId = `learned-${jobs.join("-")}-label`;
  return (
    <SettingsCard title={title} titleId={titleId} description={description}>
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
