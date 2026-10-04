"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import {
  getDelegation,
  getHandledReplies,
  setHandleIt,
  type DelegationSettings,
  type HandledReply,
  type HandleIt,
  type HandleItMode,
} from "@/lib/api";
import { formatAgo } from "@/lib/setupStatus";

// Handle it for me (PUT /delegation/handle-it) on Settings → On its own:
// replies the inbox watcher sends from your mailbox on its own. Plain code
// decides each one (delegation/handle_it.py); one setting, Careful /
// Balanced / Bold, says how much goes without you, the way quality does on
// the Agent Council. Anything it won't send waits on Today as before. Below
// the setting, what it sent in the last week (GET /delegation/handled).
export const HANDLE_IT_MODES: { mode: HandleItMode; label: string; replies: string }[] = [
  {
    mode: "careful",
    label: "Careful",
    replies: "Only short replies to people you know, when it's very sure.",
  },
  {
    mode: "balanced",
    label: "Balanced",
    replies: "Replies to people you know. Strangers, links and amounts wait for you.",
  },
  {
    mode: "bold",
    label: "Bold",
    replies: "Also strangers and longer replies, and links or amounts when it's very sure.",
  },
];

// The whole As you part of the page: nothing for someone who can't have
// Act as me (GET /delegation answers null), a pointer to Act as me until the
// inbox watcher is on, then the card.
export default function HandleItCard() {
  const [settings, setSettings] = useState<DelegationSettings | null>(null);
  const [state, setState] = useState<"loading" | "hidden" | "ready" | "error">("loading");

  useEffect(() => {
    const controller = new AbortController();
    getDelegation(controller.signal)
      .then((next) => {
        if (!next || !next.handle_it || !next.inbox) {
          setState("hidden");
          return;
        }
        setSettings(next);
        setState("ready");
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => controller.abort();
  }, []);

  if (state === "loading") return <p className="text-[15px] text-fg-muted">Loading…</p>;
  if (state === "error") {
    return (
      <SettingsCard>
        <p className="text-sm text-fg-muted">Couldn&apos;t load Handle it for me.</p>
      </SettingsCard>
    );
  }
  if (state === "hidden" || !settings?.handle_it || !settings.inbox) {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted">Nothing here yet: it needs Act as me, which this account doesn&apos;t have.</p>
      </SettingsCard>
    );
  }
  return (
    <HandleItSection handleIt={settings.handle_it} inboxOn={settings.inbox.enabled} onSettings={setSettings} />
  );
}

export function HandleItSection({
  handleIt,
  inboxOn,
  onSettings,
}: {
  handleIt: HandleIt;
  inboxOn: boolean;
  onSettings: (next: DelegationSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [handled, setHandled] = useState<HandledReply[] | null>(null);
  const on = handleIt.enabled;
  const current = HANDLE_IT_MODES.find((m) => m.mode === handleIt.mode) ?? HANDLE_IT_MODES[1];

  useEffect(() => {
    if (!on) return;
    const controller = new AbortController();
    getHandledReplies(controller.signal)
      .then(setHandled)
      .catch(() => setHandled(null));
    return () => controller.abort();
  }, [on, handleIt.sent_today]);

  const save = async (update: { enabled?: boolean; mode?: HandleItMode }) => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await setHandleIt(update));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsCard
      title="Handle it for me"
      titleId="handle-it-label"
      description={
        !handleIt.available
          ? "Needs signed sign-ins on this server before it can send anything as you."
          : on
            ? "It sends replies from your mailbox on its own and lists them here. The rest wait for you on Today."
            : inboxOn
              ? "Off: every reply waits for you to tap Send."
              : (
                  <>
                    Turn on Draft replies to my inbox in{" "}
                    <Link href="/settings/act-as-me" className="text-accent hover:underline">
                      Act as me
                    </Link>{" "}
                    first.
                  </>
                )
      }
      action={
        <Switch
          checked={on}
          onChange={() => void save({ enabled: !on })}
          disabled={busy || (!on && (!inboxOn || !handleIt.available))}
          labelledBy="handle-it-label"
        />
      }
    >
      {on && (
        <div className="flex flex-col gap-4">
          <div
            role="radiogroup"
            aria-label="How much it sends on its own"
            className="grid grid-cols-3 gap-1.5 rounded-xl bg-surface-overlay p-1"
          >
            {HANDLE_IT_MODES.map(({ mode, label }) => {
              const picked = mode === current.mode;
              return (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={picked}
                  disabled={busy}
                  onClick={() => {
                    if (!picked) void save({ mode });
                  }}
                  className={`min-h-touch rounded-lg text-[15px] font-semibold transition-colors cursor-pointer disabled:opacity-60 ${
                    picked ? "bg-surface-elevated text-fg shadow-sm" : "text-fg-muted hover:text-fg"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
          <div className="rounded-xl bg-surface-overlay/60 px-4 py-3 text-[15px] leading-relaxed">
            <p>{current.replies}</p>
            <p className="mt-1.5 text-sm text-fg-muted">
              Money, contracts, legal, hiring, the press and passwords always wait for you, and it never writes to
              anyone the email didn&apos;t go to.
            </p>
          </div>
          <p className="text-sm text-fg-muted">
            {handleIt.sent_today === 1
              ? "Sent 1 reply on its own today."
              : `Sent ${handleIt.sent_today} replies on its own today.`}
          </p>
          {handled && handled.length > 0 && (
            <ul className="flex flex-col gap-2" aria-label="Handled for you this week">
              {handled.map((h) => (
                <li key={h.decision_id} className="rounded-md border border-border px-3 py-2 text-sm">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="min-w-0 font-medium">
                      Replied to {h.to_name || h.to_email}: {h.subject}
                    </span>
                    <span className="text-xs text-fg-muted">{formatAgo(h.sent_at)}</span>
                  </div>
                  {h.open_questions.length > 0 && (
                    <p className="mt-1 text-fg-muted">Still yours to answer: {h.open_questions.join(" ")}</p>
                  )}
                  {h.gmail_link && (
                    <a href={h.gmail_link} target="_blank" rel="noreferrer" className="mt-1 inline-block text-accent">
                      Open in your mailbox
                    </a>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
