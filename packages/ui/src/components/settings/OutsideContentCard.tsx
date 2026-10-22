"use client";

import { useEffect, useId, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import {
  getOutsideContent,
  setOutsideContent,
  type OutsideContentMode,
  type OutsideContentSetting,
} from "@/lib/api";

const OPTIONS: { value: OutsideContentMode; title: string; short: string; text: string; recommended?: boolean }[] = [
  {
    value: "ask",
    title: "Careful",
    short: "Careful",
    text: "I check with you before acting on anything I read from outside your team.",
  },
  {
    value: "outsiders",
    title: "Balanced",
    short: "Balanced",
    text: "I go ahead with normal content and ask only when something looks off.",
    recommended: true,
  },
  {
    value: "trust",
    title: "Hands-off",
    short: "Hands-off",
    text: "I rarely ask. If something looks off, I skip that part and tell you.",
  },
];

// The channels by the names people know them (outside_mode.CHANNELS). An id
// the API adds later still shows, by its id.
const CHANNEL_NAMES: Record<string, string> = {
  web: "Web app",
  slack: "Slack",
  google_chat: "Google Chat",
  telegram: "Telegram",
  discord: "Discord",
  email: "Email",
};

// Settings → Your Executive: the safety level, when a read of someone
// else's text holds what the Executive does next, and the channels set apart
// from it (orchestrator/outside_mode.py). Every read is checked for tricks
// whatever the level (orchestrator/content_scan.py). Shown to the owner
// only; nobody else may change it.
export default function OutsideContentCard() {
  const [setting, setSetting] = useState<OutsideContentSetting | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const panelId = useId();

  useEffect(() => {
    const ctrl = new AbortController();
    getOutsideContent(ctrl.signal)
      .then((s) => {
        setSetting(s);
        // Open the channels when one is already set apart, so it shows.
        if (Object.keys(s.channels ?? {}).length > 0) setOpen(true);
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setError("Couldn't load this setting.");
      });
    return () => ctrl.abort();
  }, []);

  if (setting && !setting.can_change) return null;

  const save = async (mode: OutsideContentMode, channels?: Record<string, OutsideContentMode | null>) => {
    if (!setting || busy) return;
    setBusy(true);
    setError(null);
    try {
      setSetting(await setOutsideContent(mode, channels));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  // Picking a level for everything clears the channels set apart.
  const pickAll = (mode: OutsideContentMode) => {
    if (!setting) return;
    const cleared = Object.fromEntries(Object.keys(setting.channels ?? {}).map((c) => [c, null]));
    if (mode === setting.mode && Object.keys(cleared).length === 0) return;
    void save(mode, cleared);
  };

  // A channel set to the company's level follows it again.
  const pickChannel = (channel: string, mode: OutsideContentMode) => {
    if (!setting) return;
    const current = setting.channels?.[channel] ?? setting.mode;
    if (mode === current) return;
    void save(setting.mode, { [channel]: mode === setting.mode ? null : mode });
  };

  const custom = setting ? Object.keys(setting.channels ?? {}).length > 0 : false;

  return (
    <SettingsCard
      title="When should I check with you?"
      description="I check every email, file and web page I read for tricks, and flag anything that looks off. Choose when I should stop and ask you."
    >
      {!setting && !error && <p className="text-[15px] text-fg-muted">Loading…</p>}
      {setting && (
        <div className="flex flex-col gap-4">
          <div role="radiogroup" aria-label="When should I check with you?" className="flex flex-col gap-2">
            {OPTIONS.map((o) => {
              const picked = !custom && o.value === setting.mode;
              return (
                <button
                  key={o.value}
                  type="button"
                  role="radio"
                  aria-checked={picked}
                  disabled={busy}
                  onClick={() => pickAll(o.value)}
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
                    <span className="flex items-center gap-2">
                      <span className="text-[15px] font-semibold text-fg">{o.title}</span>
                      {o.recommended && (
                        <span className="rounded-full bg-accent/15 px-2 py-0.5 text-xs font-semibold text-accent-strong">
                          Recommended
                        </span>
                      )}
                    </span>
                    <span className="block text-sm text-fg-muted">{o.text}</span>
                  </span>
                </button>
              );
            })}
            {custom && (
              <p className="text-sm text-fg-muted">Custom: some channels are set differently below.</p>
            )}
          </div>

          <p className="text-sm text-fg-muted">
            Whatever you pick, I only email people in People, mail from strangers reaches you as an alert, and chats
            with other people in them always get Careful.
          </p>

          <div className="border-t border-line pt-3">
            <button
              type="button"
              aria-expanded={open}
              aria-controls={panelId}
              onClick={() => setOpen((v) => !v)}
              className="flex min-h-touch w-full cursor-pointer items-center justify-between text-left text-[15px] font-semibold text-fg"
            >
              <span>Set each channel</span>
              <span aria-hidden="true" className="text-fg-muted">
                {open ? "−" : "+"}
              </span>
            </button>
            {open && (
              <ul id={panelId} className="flex flex-col">
                {setting.channel_ids.map((channel) => {
                  const current = setting.channels?.[channel] ?? setting.mode;
                  const name = CHANNEL_NAMES[channel] ?? channel;
                  return (
                    <li
                      key={channel}
                      className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b border-line py-3 last:border-b-0"
                    >
                      <span className="text-[15px] text-fg">{name}</span>
                      <div role="radiogroup" aria-label={name} className="flex gap-1 rounded-xl border border-line p-1">
                        {OPTIONS.map((o) => {
                          const picked = o.value === current;
                          return (
                            <button
                              key={o.value}
                              type="button"
                              role="radio"
                              aria-checked={picked}
                              disabled={busy}
                              onClick={() => pickChannel(channel, o.value)}
                              className={`min-h-touch rounded-lg px-3 text-sm font-medium transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                                picked ? "bg-accent-strong text-white" : "text-fg-muted hover:bg-surface-overlay/60"
                              }`}
                            >
                              {o.short}
                            </button>
                          );
                        })}
                      </div>
                    </li>
                  );
                })}
                <li className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 py-3">
                  <span className="text-[15px] text-fg">Chats with other people in them</span>
                  <span className="text-sm text-fg-muted">Always Careful</span>
                </li>
              </ul>
            )}
          </div>
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
