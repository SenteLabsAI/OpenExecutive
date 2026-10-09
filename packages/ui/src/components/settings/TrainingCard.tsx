"use client";

import { useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import {
  removeTrainingLearned,
  setTraining,
  type DelegationSettings,
  type Training,
  type TrainingLearned,
  type TrainingSetting,
} from "@/lib/api";

// Training on Settings → Act as me (PUT /delegation/training): each Act as
// me setting can be put in training on its own (delegation/training.py).
// In training it asks you first, and learns who and how from what you
// approve: Send + allow on a reply or follow-up, Approve + allow on a
// suggested action, and "Do it like this next time" on a draft you changed.
// Below the switches, one "What it's learned" list, each item labelled by
// its setting, with Remove (DELETE /delegation/learned/{id}).
export const TRAINING_SETTINGS: { setting: TrainingSetting; label: string; text: string }[] = [
  {
    setting: "replies",
    label: "Replies to my inbox",
    text: "Every reply waits. Send + allow lets replies to that person go on their own.",
  },
  {
    setting: "follow_ups",
    label: "Follow-ups",
    text: "Every follow-up waits. Send + allow lets follow-ups to those people go.",
  },
  {
    setting: "actions",
    label: "Suggested actions",
    text: "Invites and messages from your mail wait. Approve + allow lets that kind, with those people, happen on its own.",
  },
  {
    setting: "drafts",
    label: "Drafts",
    text: "Nothing is sent. Your edits teach it how you write to each person.",
  },
];

const SETTING_NAMES: Record<TrainingSetting, string> = {
  replies: "Replies",
  follow_ups: "Follow-ups",
  actions: "Suggested actions",
  drafts: "Drafts",
};

function times(n: number): string {
  return n === 1 ? "once" : n === 2 ? "twice" : `${n} times`;
}

function learnedLine(item: TrainingLearned): string {
  const when = new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const parts = [SETTING_NAMES[item.setting] ?? "Act as me"];
  parts.push(item.setting === "drafts" ? `learned ${when}` : `allowed ${when}`);
  if (item.uses > 0) parts.push(`done ${times(item.uses)} since`);
  if (item.example) parts.push(`writes like “${item.example}”`);
  return parts.join(" · ");
}

export default function TrainingSection({
  training,
  onSettings,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
}) {
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

  const anyOn = TRAINING_SETTINGS.some((s) => training[s.setting]);
  const learned = training.learned;

  return (
    <SettingsCard
      title="Training"
      titleId="act-as-me-training-label"
      description="Put any of these in training. Each one asks you first, and learns who and how from what you approve."
    >
      <div className="flex flex-col gap-4">
        <ul className="flex flex-col divide-y divide-line rounded-xl border border-line">
          {TRAINING_SETTINGS.map((s) => {
            const id = `act-as-me-training-${s.setting}`;
            return (
              <li key={s.setting} className="flex min-h-touch items-center justify-between gap-3 px-4 py-3">
                <span className="min-w-0">
                  <span id={id} className="block text-[15px] font-semibold text-fg">
                    {s.label}
                  </span>
                  <span className="mt-0.5 block text-sm leading-snug text-fg-muted">{s.text}</span>
                </span>
                <Switch
                  checked={training[s.setting]}
                  onChange={(next) => void run(() => setTraining({ [s.setting]: next }), "Could not change training.")}
                  disabled={busy}
                  labelledBy={id}
                />
              </li>
            );
          })}
        </ul>
        {(anyOn || learned.length > 0) && (
          <div>
            <h3 className="text-[15px] font-semibold text-fg">What it&apos;s learned</h3>
            <p className="mt-1 text-sm text-fg-muted">
              Yours alone. Remove one and it asks you again.
            </p>
            {learned.length === 0 ? (
              <p className="mt-3 text-sm text-fg-muted">Nothing yet.</p>
            ) : (
              <ul className="mt-3 flex flex-col divide-y divide-line rounded-xl border border-line">
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
          </div>
        )}
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
