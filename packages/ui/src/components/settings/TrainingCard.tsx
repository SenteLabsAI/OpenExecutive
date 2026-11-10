"use client";

import { useState, type ReactNode } from "react";

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
// in How it learns from you (Delegate → As you) while its switch is on or
// something is on the list. Remove one and it asks again.

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
  if (item.uses > 0) parts.push(`used ${times(item.uses)}`);
  if (item.example) parts.push(`writes like “${item.example}”`);
  return parts.join(", ");
}

// One part of How it learns from you (Delegate → As you): a small label
// over a line, then what's in it.
export function LearnPart({
  title,
  titleId,
  action,
  children,
}: {
  title: string;
  titleId?: string;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="border-t border-line pt-5">
      <div className="flex items-start justify-between gap-4">
        <h3 id={titleId} className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">
          {title}
        </h3>
        {action && <div className="flex-shrink-0">{action}</div>}
      </div>
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}

// What In training kept, one kind at a time: who it may send to on its own
// ("training") or how you write to each person ("drafts"). Nothing to show
// (no list, and the switch that fills it is off) shows nothing.
export function LearnedSection({
  training,
  onSettings,
  setting,
  title,
  empty,
}: {
  training: Training;
  onSettings: (next: DelegationSettings) => void;
  setting: "training" | "drafts";
  title: string;
  empty: string;
}) {
  const { busy, error, run } = useSave(onSettings);
  const learned = training.learned.filter((item) => item.setting === setting);
  if (learned.length === 0 && !training[setting]) return null;
  const titleId = `learned-${setting}-label`;
  return (
    <LearnPart title={title} titleId={titleId}>
      {learned.length === 0 ? (
        <p className="text-sm text-fg-muted">{empty}</p>
      ) : (
        <ul aria-labelledby={titleId} className="flex flex-col divide-y divide-line">
          {learned.map((item) => (
            <li key={item.id} className="flex min-h-touch items-center justify-between gap-3 py-1.5">
              <span className="min-w-0 text-[15px] leading-snug">
                <span className="font-semibold break-words">{item.label}</span>
                <span className="text-fg-muted"> · {learnedLine(item)}</span>
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
    </LearnPart>
  );
}
