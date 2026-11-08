"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import ModePicker from "@/components/settings/ModePicker";
import SettingsCard from "@/components/settings/SettingsCard";
import Button from "@/components/ui/Button";
import {
  getDelegation,
  getOutsideContent,
  setReach,
  setTraining,
  type DelegationSettings,
  type OutsideContentMode,
} from "@/lib/api";
import {
  APPROVALS_SENDS,
  SENDS_ANCHOR,
  WRITES_ANCHOR,
  reachForSend,
  sendOf,
  writeOf,
  type SendStep,
  type WriteStep,
} from "@/lib/reach";

// What it writes and sends as you, from your own mailbox: one setting on the
// server (PUT /delegation/reach) shown as two pickers. WritesCard, on
// Delegate → Act as me, is the steps that write and never send. SendsCard,
// on Delegate → Handle it for me, is one switch: what goes on its own is the
// Approvals level's call (Settings → Your Executive). Handle it for me
// builds on the inbox replies, so SendsCard opens only once WritesCard is at
// Draft replies automatically, and moving WritesCard below that says Handle it for me
// stops too. Moving either down says what stops before it saves.

type Step<T extends string> = { value: T; label: string; line: string; reason?: string | null };

function useReachSave(onSettings: (next: DelegationSettings) => void) {
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

// Linked from another tab (WRITES_HREF / SENDS_HREF): the card appears after
// the settings load, so the browser's own jump to the anchor has passed it.
function useJumpTo(anchor: string) {
  useEffect(() => {
    if (window.location.hash === `#${anchor}`) {
      document.getElementById(anchor)?.scrollIntoView({ block: "start" });
    }
  }, [anchor]);
}

function StepList<T extends string>({
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
  return (
    <div role="radiogroup" aria-labelledby={labelledBy} className="flex flex-col gap-2">
      {steps.map((step) => {
        const checked = step.value === current;
        const reason = step.reason;
        return (
          <label
            key={step.value}
            className={`flex items-start gap-3 rounded-xl border p-3.5 transition-colors ${
              checked ? "border-accent/60 bg-accent/10" : "border-line bg-surface-elevated hover:border-line-strong"
            } ${reason && !checked ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
          >
            <input
              type="radio"
              name={name}
              value={step.value}
              checked={checked}
              disabled={busy || (Boolean(reason) && !checked)}
              onChange={() => onPick(step.value)}
              className="mt-1 h-4 w-4 flex-shrink-0 accent-indigo-500"
            />
            <span className="min-w-0">
              <span className="block text-[15px] font-medium text-fg">{step.label}</span>
              <span className="mt-0.5 block text-sm text-fg-muted leading-snug">
                {reason && !checked ? reason : step.line}
              </span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

function ConfirmDown({
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

function Check({
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

const WRITE_ORDER: WriteStep[] = ["off", "when_asked", "inbox"];

// Delegate → Act as me: what it writes as you. `inbox` (the inbox watcher's
// status, Check now and Also save in Drafts) shows under Draft replies automatically.
export function WritesCard({
  settings,
  onSettings,
  inbox,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
  inbox?: ReactNode;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const [confirming, setConfirming] = useState<WriteStep | null>(null);
  useJumpTo(WRITES_ANCHOR);

  const mailbox = settings.gmail?.provider === "microsoft" ? "Outlook" : "Gmail";
  const connected = settings.gmail?.status === "connected";
  const current = writeOf(settings);
  const sending = sendOf(settings) !== "off";
  const needsMailbox = !connected && current === "off" ? "Connect your mailbox first." : null;
  const steps: Step<WriteStep>[] = [
    { value: "off", label: "Off", line: "The Executive only writes as itself." },
    {
      value: "when_asked",
      label: "When I ask",
      line: `Ask in chat, “reply to Dana as me: yes to the 5th”, and the draft waits in your ${mailbox} Drafts.`,
      reason: needsMailbox,
    },
    {
      value: "inbox",
      label: "Draft replies automatically",
      line: "It also writes a first reply to mail that needs you and puts it on Home.",
      reason: needsMailbox,
    },
  ];

  const pick = (to: WriteStep) => {
    if (to === current) return;
    if (WRITE_ORDER.indexOf(to) < WRITE_ORDER.indexOf(current)) {
      setConfirming(to);
      return;
    }
    void save(() => setReach(to));
  };

  const stops = (to: WriteStep): string => {
    const handleIt = sending ? " Handle it for me stops sending too." : "";
    if (to === "when_asked") return `It stops writing replies to your inbox.${handleIt}`;
    return `It stops writing anything as you, and turns Read my calendar off too.${handleIt}`;
  };

  return (
    <div id={WRITES_ANCHOR} className="scroll-mt-24">
      <SettingsCard title="What it writes as you" titleId="writes-label" description="Nothing goes out until you send it.">
        <StepList name="writes" labelledBy="writes-label" steps={steps} current={current} busy={busy} onPick={pick} />
        {confirming && (
          <ConfirmDown
            text={`Move to ${steps.find((s) => s.value === confirming)?.label}? ${stops(confirming)}`}
            busy={busy}
            onConfirm={() => void save(() => setReach(confirming)).then((ok) => ok && setConfirming(null))}
            onCancel={() => setConfirming(null)}
          />
        )}
        {current === "inbox" && inbox && <div className="mt-4">{inbox}</div>}
        {current !== "off" && (
          <Check
            className="mt-5 border-t border-line pt-4"
            checked={Boolean(settings.training?.drafts)}
            disabled={busy}
            onChange={(learn) => void save(() => setTraining({ drafts: learn }))}
            label="Learn from my edits"
            help="When you change a draft before sending, tick Do it like this next time and it learns how you write to that person. Nothing more is sent."
          />
        )}
        {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
      </SettingsCard>
    </div>
  );
}

// Delegate → Handle it for me: one switch. What goes on its own is the
// Approvals level's (delegation/handle_it.py approvals_hold), shown here
// with a link to change it.
export function SendsCard({
  settings,
  onSettings,
}: {
  settings: DelegationSettings;
  onSettings: (next: DelegationSettings) => void;
}) {
  const { busy, error, save } = useReachSave(onSettings);
  const [confirming, setConfirming] = useState(false);
  const [level, setLevel] = useState<OutsideContentMode | null>(null);
  useJumpTo(SENDS_ANCHOR);

  useEffect(() => {
    const controller = new AbortController();
    getOutsideContent(controller.signal)
      .then((next) => setLevel(next.mode))
      .catch(() => setLevel(null));
    return () => controller.abort();
  }, []);

  const connected = settings.gmail?.status === "connected";
  const writesInbox = writeOf(settings) === "inbox";
  const current = sendOf(settings);
  const canSend = Boolean(settings.handle_it?.available);
  const levelText = level ? APPROVALS_SENDS[level] : null;

  const pick = (to: SendStep) => {
    if (to === current) return;
    if (to === "off") {
      setConfirming(true);
      return;
    }
    setConfirming(false);
    void save(() => setReach(reachForSend("on")));
  };

  return (
    <div id={SENDS_ANCHOR} className="scroll-mt-24">
      <SettingsCard
        title="What it sends on its own"
        titleId="sends-label"
        description="It answers from the replies Act as me writes for your inbox."
      >
        {!writesInbox ? (
          <div className="flex flex-col items-start gap-3">
            <p className="text-sm text-fg-muted leading-relaxed">
              It needs Act as me to write replies for your inbox first.
            </p>
            {connected ? (
              <Button variant="primary" disabled={busy} onClick={() => void save(() => setReach("inbox"))}>
                {busy ? "Turning on…" : "Turn on Draft replies automatically"}
              </Button>
            ) : (
              <Link
                href="/delegate/act-as-me"
                className="inline-flex h-10 items-center rounded-lg border border-line bg-surface px-4 text-sm font-semibold text-accent hover:border-line-strong"
              >
                Connect your mailbox in Act as me
              </Link>
            )}
          </div>
        ) : (
          <>
            <ModePicker<SendStep>
              labelledBy="sends-label"
              value={current}
              disabled={busy}
              options={[
                { value: "off", label: "Off" },
                { value: "on", label: "On", disabled: !canSend },
              ]}
              onPick={pick}
            />
            <p className="mt-3 text-sm text-fg-muted leading-relaxed">
              {current === "off"
                ? canSend
                  ? "Every reply waits for you to send."
                  : "Needs signed sign-ins on this server."
                : levelText
                  ? `Approvals is on ${levelText.label}. ${levelText.line}`
                  : "Approvals decides what goes on its own."}
            </p>
            {current === "on" && (
              <Link href="/settings/executive" className="mt-1 inline-block text-sm font-semibold text-accent hover:underline">
                Change Approvals
              </Link>
            )}
            {confirming && (
              <ConfirmDown
                text="Turn Handle it for me off? It stops sending replies and follow-ups on its own. Every reply waits for you."
                busy={busy}
                onConfirm={() =>
                  void save(() => setReach(reachForSend("off"))).then((ok) => ok && setConfirming(false))
                }
                onCancel={() => setConfirming(false)}
              />
            )}
            <p className="mt-5 border-t border-line pt-4 text-sm text-fg-muted">
              It never writes to anyone the email didn&apos;t go to.
            </p>
          </>
        )}
        {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
      </SettingsCard>
    </div>
  );
}
