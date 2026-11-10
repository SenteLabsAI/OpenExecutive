"use client";

import { useState, type ReactNode } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import { LearnedSection } from "@/components/settings/TrainingCard";
import Switch from "@/components/Switch";
import {
  getDelegation,
  setReach,
  setTraining,
  type DelegationSettings,
  type OutsideContentMode,
  type ReachStep,
} from "@/lib/api";
import { REACH_ORDER, reachLine, reachOf } from "@/lib/reach";

// Delegate → As you: how far it goes as you, from your own mailbox. One
// setting on the server (PUT /delegation/reach): Off, When you ask, Your
// inbox. What goes on its own from the inbox is the Approvals level's call,
// held back while In training is on; there is no switch for it. Moving down
// says what stops before it saves.

type Step<T extends string> = { value: T; label: string; line: string; reason?: string | null };

export function useReachSave(onSettings: (next: DelegationSettings) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async (work: () => Promise<DelegationSettings>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await work());
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
      // A refused move up can stop part way, at a lower step: show where it is.
      try {
        const now = await getDelegation();
        if (now) onSettings(now);
      } catch {
        // Keep what's shown; the error above already says it didn't save.
      }
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, save };
}

// One row of choices, side by side. The picked one's line (or why a step
// can't be picked yet) shows under it.
export function StepList<T extends string>({
  name,
  labelledBy,
  steps,
  current,
  busy,
  onPick,
}: {
  name: string;
  labelledBy: string;
  steps: Step<T>[];
  current: T;
  busy: boolean;
  onPick: (step: T) => void;
}) {
  const picked = steps.find((step) => step.value === current);
  const blocked = steps.find((step) => step.reason && step.value !== current)?.reason;
  return (
    <div>
      <div
        role="radiogroup"
        aria-labelledby={labelledBy}
        className="inline-flex max-w-full flex-wrap overflow-hidden rounded-xl border border-line"
      >
        {steps.map((step, i) => {
          const checked = step.value === current;
          const off = Boolean(step.reason) && !checked;
          return (
            <label
              key={step.value}
              title={off ? (step.reason ?? undefined) : undefined}
              className={`relative flex min-h-touch items-center px-3.5 text-sm transition-colors ${
                i > 0 ? "border-l border-line" : ""
              } ${checked ? "bg-accent/10 font-semibold text-accent-strong" : "text-fg-muted hover:bg-surface-overlay/60"} ${
                off ? "cursor-not-allowed opacity-60" : "cursor-pointer"
              } has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-accent`}
            >
              <input
                type="radio"
                name={name}
                value={step.value}
                checked={checked}
                disabled={busy || off}
                onChange={() => onPick(step.value)}
                className="sr-only"
              />
              {step.label}
            </label>
          );
        })}
      </div>
      {picked && <p className="mt-2 text-sm text-fg-muted leading-snug">{picked.line}</p>}
      {blocked && <p className="mt-1 text-sm text-fg-subtle leading-snug">{blocked}</p>}
    </div>
  );
}

export function ConfirmDown({
  text,
  busy,
  onConfirm,
  onCancel,
}: {
  text: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mt-4 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4">
      <p className="text-sm text-fg leading-relaxed">{text}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onConfirm}
          className="h-9 rounded-lg bg-fg px-3 text-sm font-medium text-fg-inverse disabled:opacity-50"
        >
          {busy ? "Saving…" : "Move down"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="h-9 rounded-lg border border-line px-3 text-sm font-medium text-fg"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export function Check({
  checked,
  disabled,
  onChange,
  label,
  help,
  className = "",
}: {
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
  label: string;
  help: string;
  className?: string;
}) {
  return (
    <label className={`flex cursor-pointer items-start gap-3 ${className}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-5 w-5 flex-shrink-0 accent-indigo-500"
      />
      <span>
        <span className="block text-[15px] font-semibold text-fg">{label}</span>
        <span className="mt-0.5 block text-sm text-fg-muted leading-snug">{help}</span>
      </span>
    </label>
  );
}

// The As you picker. `mailbox` (which mailbox it writes from, with Change,
// or the buttons that connect one) is the card's first line, since every
// choice below depends on it. `inbox` (the inbox watcher's status, Check now
// and Also save in Drafts) shows under Your inbox; `level` is the Approvals
// level, for the "Now:" line.
export function ReachCard({
  settings,
  onSettings,
  mailbox: mailboxLine,
  inbox,
  level,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
  mailbox?: ReactNode;
  inbox?: ReactNode;
  level: OutsideContentMode | null;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const [confirming, setConfirming] = useState<ReachStep | null>(null);

  const mailbox = settings.gmail?.provider === "microsoft" ? "Outlook" : "Gmail";
  const connected = settings.gmail?.status === "connected";
  const current = reachOf(settings);
  const inTraining = Boolean(settings.training?.training);
  const needsMailbox = !connected && current === "off" ? "Connect your mailbox first." : null;
  const steps: Step<ReachStep>[] = [
    { value: "off", label: "Off", line: "It never writes as you." },
    {
      value: "when_asked",
      label: "When you ask",
      line: `Ask in chat, “reply to Dana as me: yes to the 5th”, and the draft waits in your ${mailbox} Drafts. You send it.`,
      reason: needsMailbox,
    },
    {
      value: "inbox",
      label: "Your inbox",
      line: "For each email that needs you, it does what the email asks where it can, writes the reply and puts it on Home. Approvals decides which go on their own.",
      reason: needsMailbox,
    },
  ];

  const pick = (to: ReachStep) => {
    if (to === current) return;
    if (REACH_ORDER.indexOf(to) < REACH_ORDER.indexOf(current)) {
      setConfirming(to);
      return;
    }
    void save(() => setReach(to));
  };

  const stops = (to: ReachStep): string =>
    to === "when_asked"
      ? "It stops drafting replies to your inbox, and nothing more goes on its own as you."
      : "It stops writing anything as you, and turns Read your calendar off too.";

  return (
    <SettingsCard title="How far it goes as you" titleId="reach-label">
      {mailboxLine && <div className="mb-4">{mailboxLine}</div>}
      <StepList name="reach" labelledBy="reach-label" steps={steps} current={current} busy={busy} onPick={pick} />
      <p className="mt-3 text-[15px] text-fg">
        <span className="text-fg-muted">Now: </span>
        {reachLine(current, inTraining, level)}
      </p>
      {confirming && (
        <ConfirmDown
          text={`Move to ${steps.find((s) => s.value === confirming)?.label}? ${stops(confirming)}`}
          busy={busy}
          onConfirm={() => void save(() => setReach(confirming)).then((ok) => ok && setConfirming(null))}
          onCancel={() => setConfirming(null)}
        />
      )}
      {current === "inbox" && inbox && <div className="mt-4">{inbox}</div>}
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

// In training for As you (PUT /delegation/training {training}): while it is
// on, nothing goes out as you on its own, on any Approvals level, until you
// tick "Send to them on its own next time" on a card. Its own card at the top
// of the As you tab's second column, the way As me shows its In training:
// the switch, who goes on their own (with Remove) and Learn from my edits.
// Shown once it writes as you at all, or while someone is still on its list.
export function AsYouTrainingCard({
  settings,
  onSettings,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const training = settings.training;
  if (!training) return null;
  if (!settings.enabled && !training.learned.some((item) => item.setting === "training")) return null;
  const on = Boolean(training.training);
  return (
    <SettingsCard
      tone="accent"
      title="In training"
      titleId="as-you-training-label"
      description={
        on
          ? "Everything it would send as you comes to you first as a card on Home, whatever Approvals says. Tick “on its own next time” when you send one and that person goes on their own from then on."
          : "Off: Approvals alone decides what goes on its own as you."
      }
      action={
        <Switch
          checked={on}
          disabled={busy}
          labelledBy="as-you-training-label"
          onChange={(next) => void save(() => setTraining({ training: next }))}
        />
      }
    >
      <div className="space-y-5">
        <LearnedSection
          training={training}
          onSettings={onSettings}
          setting="training"
          title="Goes on its own"
          empty="Nobody yet. Tick “on its own next time” when you send one."
        />
        <div className="border-t border-line pt-5">
          <Check
            checked={Boolean(training.drafts)}
            disabled={busy}
            onChange={(learn) => void save(() => setTraining({ drafts: learn }))}
            label="Learn from my edits"
            help="Change a draft, tick Do it like this next time, and it learns how you write to that person."
          />
        </div>
      </div>
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
