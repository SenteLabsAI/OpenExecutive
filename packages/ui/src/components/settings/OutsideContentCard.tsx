"use client";

import { useEffect, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import {
  getOutsideContent,
  setOutsideContent,
  type OutsideContentMode,
  type OutsideContentSetting,
} from "@/lib/api";

const OPTIONS: { value: OutsideContentMode; title: string; text: string }[] = [
  {
    value: "ask",
    title: "Ask every time",
    text: "After I read any email, file or page, I check with you before I change or send anything.",
  },
  {
    value: "outsiders",
    title: "Only for outsiders",
    text: "Your calendar, your team's mail, files you send me and company folders don't stop me. Mail from anyone else and web pages do.",
  },
  {
    value: "trust",
    title: "Don't ask",
    text: "I act in your own chat without checking. Email still goes only to People.",
  },
];

// Settings → Your Executive: when a read of someone else's text holds what it
// does next (orchestrator/outside_mode.py). Shown to the owner only; nobody
// else may change it.
export default function OutsideContentCard() {
  const [setting, setSetting] = useState<OutsideContentSetting | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    getOutsideContent(ctrl.signal)
      .then(setSetting)
      .catch(() => {
        if (!ctrl.signal.aborted) setError("Couldn't load this setting.");
      });
    return () => ctrl.abort();
  }, []);

  if (setting && !setting.can_change) return null;

  const pick = async (mode: OutsideContentMode) => {
    if (!setting || busy || mode === setting.mode) return;
    setBusy(true);
    setError(null);
    try {
      setSetting(await setOutsideContent(mode));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsCard
      title="When I read outside content"
      description="Text someone else wrote, in an email, file or web page, can try to tell me what to do. Pick when I check with you before acting on it."
    >
      {!setting && !error && <p className="text-[15px] text-fg-muted">Loading…</p>}
      {setting && (
        <div role="radiogroup" aria-label="When I read outside content" className="flex flex-col gap-2">
          {OPTIONS.map((o) => {
            const picked = o.value === setting.mode;
            return (
              <button
                key={o.value}
                type="button"
                role="radio"
                aria-checked={picked}
                disabled={busy}
                onClick={() => pick(o.value)}
                className={`flex min-h-touch gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                  picked ? "border-accent-strong bg-accent/10" : "border-line hover:bg-surface-overlay/60"
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`mt-1 h-4 w-4 flex-shrink-0 rounded-full ${
                    picked ? "border-[5px] border-accent-strong" : "border-2 border-fg-subtle"
                  }`}
                />
                <span>
                  <span className="block text-[15px] font-semibold text-fg">{o.title}</span>
                  <span className="block text-sm text-fg-muted">{o.text}</span>
                </span>
              </button>
            );
          })}
          <p className="mt-1 text-sm text-fg-muted">
            Before I email someone new, I always ask you first. Only you can change this.
          </p>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </SettingsCard>
  );
}
