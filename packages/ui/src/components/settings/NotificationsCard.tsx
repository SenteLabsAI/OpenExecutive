"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import Button from "@/components/ui/Button";
import { getNotificationSettings, updateNotificationSettings, type NotificationSettings } from "@/lib/api";
import {
  DAYS_LABEL,
  DELIVERY_LABEL,
  digestDescription,
  heldLine,
  quietError,
  quietSummary,
  RIGHT_NOW_LABEL,
  rightNowDescription,
  toUpdate,
  type Delivery,
  type RightNowPings,
  type QuietDays,
  type QuietHours,
} from "@/lib/notifications";

// Settings → Notifications (GET/PUT /notifications/settings): the signed-in
// person's own quiet hours, whether things reach them as they happen or in
// their briefs, and whether urgent things get through. Every change saves at
// once except the quiet-hours times, which have a Save button so a half-typed
// time is never stored.

const INPUT =
  "w-full h-11 px-3 rounded-xl text-[15px] bg-surface border border-line text-fg focus:outline-none focus:border-line-strong disabled:opacity-60";

function browserTimeZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

type Loaded = NotificationSettings | null | "loading" | "error";

export default function NotificationsCard() {
  const [state, setState] = useState<Loaded>("loading");
  const [draft, setDraft] = useState<QuietHours | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const browserZone = useMemo(() => browserTimeZone(), []);

  useEffect(() => {
    const ctrl = new AbortController();
    getNotificationSettings(ctrl.signal)
      .then((s) => {
        setState(s);
        if (s) setDraft(s.quiet_hours);
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => ctrl.abort();
  }, []);

  if (state === "loading") return null;
  if (state === "error") {
    return (
      <SettingsCard title="Notifications">
        <p className="text-[15px] text-fg-muted">Couldn&apos;t load your notification settings.</p>
      </SettingsCard>
    );
  }
  if (state === null) {
    return (
      <SettingsCard title="Notifications">
        <p className="text-[15px] text-fg-muted">
          Notification settings are for people on the People list. Sign in as one of them to set yours.
        </p>
      </SettingsCard>
    );
  }

  const current = state;
  const quiet = draft ?? current.quiet_hours;
  const dirty = JSON.stringify(quiet) !== JSON.stringify(current.quiet_hours);
  const draftError = quietError(quiet);

  async function save(
    change: Partial<Pick<NotificationSettings, "quiet_hours" | "delivery" | "urgent_through" | "right_now" | "timezone">>,
  ) {
    setBusy(true);
    setError(null);
    try {
      const next = await updateNotificationSettings(toUpdate({ ...current, ...change }, browserZone));
      setState(next);
      setDraft(next.quiet_hours);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save the change.");
    } finally {
      setBusy(false);
    }
  }

  const held = heldLine(current.held_count, current.delivery);
  const zone = current.effective_timezone;

  return (
    <>
      {error && (
        <p role="alert" className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-500">
          {error}
        </p>
      )}
      {held && (
        <p role="status" className="rounded-xl border border-line bg-surface px-4 py-3 text-[15px] text-fg">
          {held}
        </p>
      )}

      <SettingsCard
        title="Quiet hours"
        titleId="quiet-hours-label"
        description={quietSummary(current.quiet_hours)}
        action={
          <Switch
            checked={current.quiet_hours.enabled}
            labelledBy="quiet-hours-label"
            disabled={busy}
            onChange={(enabled) => void save({ quiet_hours: { ...quiet, enabled } })}
          />
        }
      >
        {current.quiet_hours.enabled && (
          <div className="space-y-4">
            <p className="text-[15px] text-fg-muted leading-relaxed">
              What the Executive would send you on its own (nudges, check-ins on your work, alerts) waits
              until your quiet hours end. Replies when you message it still come straight away.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <label className="flex flex-col gap-1.5 text-sm font-medium text-fg">
                From
                <input
                  type="time"
                  value={quiet.start}
                  disabled={busy}
                  onChange={(e) => setDraft({ ...quiet, start: e.target.value })}
                  className={INPUT}
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-fg">
                To
                <input
                  type="time"
                  value={quiet.end}
                  disabled={busy}
                  onChange={(e) => setDraft({ ...quiet, end: e.target.value })}
                  className={INPUT}
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-fg">
                Days
                <select
                  value={quiet.days}
                  disabled={busy}
                  onChange={(e) => setDraft({ ...quiet, days: e.target.value as QuietDays })}
                  className={INPUT}
                >
                  {(Object.keys(DAYS_LABEL) as QuietDays[]).map((d) => (
                    <option key={d} value={d}>
                      {DAYS_LABEL[d]}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {draftError && dirty && <p className="text-sm text-red-500">{draftError}</p>}
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="primary"
                disabled={busy || !dirty || draftError !== null}
                onClick={() => void save({ quiet_hours: quiet })}
              >
                {busy ? "Saving…" : "Save quiet hours"}
              </Button>
              {dirty && (
                <Button variant="ghost" disabled={busy} onClick={() => setDraft(current.quiet_hours)}>
                  Cancel
                </Button>
              )}
            </div>
            <p className="text-[13px] text-fg-subtle">
              Times are in {zone}.
              {browserZone && browserZone !== zone && (
                <>
                  {" "}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void save({ timezone: browserZone })}
                    className="font-medium text-accent hover:underline cursor-pointer disabled:opacity-50"
                  >
                    Use {browserZone} instead
                  </button>
                </>
              )}
            </p>
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        title="How things reach you"
        titleId="delivery-label"
        description="For what the Executive sends you on its own: nudges, check-ins on your work and alerts."
      >
        <div role="radiogroup" aria-labelledby="delivery-label" className="flex flex-col gap-2">
          {(["instant", "digest"] as Delivery[]).map((d) => {
            const selected = current.delivery === d;
            return (
              <button
                key={d}
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={busy}
                onClick={() => {
                  if (!selected) void save({ delivery: d });
                }}
                className={`w-full rounded-xl border px-4 py-3 text-left transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                  selected ? "border-accent bg-accent/5" : "border-line bg-surface hover:border-line-strong"
                }`}
              >
                <span className="block text-[15px] font-semibold text-fg">{DELIVERY_LABEL[d]}</span>
                <span className="mt-0.5 block text-sm text-fg-muted leading-relaxed">
                  {d === "instant" ? "Sent to you as soon as there's something." : digestDescription(current.gets_briefs)}
                </span>
              </button>
            );
          })}
        </div>
      </SettingsCard>

      <SettingsCard
        title="Let urgent things through"
        titleId="urgent-through-label"
        description={
          current.urgent_through
            ? "On: reminders about things waiting on your approval, and critical alerts, reach you right away, even in quiet hours or when you get things in your briefs."
            : "Off: those wait with everything else. A question a workflow is waiting on you to answer always comes straight away."
        }
        action={
          <Switch
            checked={current.urgent_through}
            labelledBy="urgent-through-label"
            disabled={busy}
            onChange={(urgent_through) => void save({ urgent_through })}
          />
        }
      />

      {current.gets_briefs && current.right_now && (
        <SettingsCard
          title="Tell me when Right now changes"
          titleId="right-now-label"
          description="A short message on the chat app you last used with the Executive. Never during quiet hours, or when things come in your briefs."
        >
          <div role="radiogroup" aria-labelledby="right-now-label" className="grid grid-cols-3 overflow-hidden rounded-xl border border-line">
            {(Object.keys(RIGHT_NOW_LABEL) as RightNowPings[]).map((choice) => {
              const selected = current.right_now === choice;
              return (
                <button
                  key={choice}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  disabled={busy}
                  onClick={() => {
                    if (!selected) void save({ right_now: choice });
                  }}
                  className={`min-h-[44px] px-2 py-2 text-sm transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                    selected ? "bg-accent/10 font-semibold text-accent" : "bg-surface text-fg-muted hover:text-fg"
                  }`}
                >
                  {RIGHT_NOW_LABEL[choice]}
                </button>
              );
            })}
          </div>
          <p className="mt-3 text-sm text-fg-muted leading-relaxed">{rightNowDescription(current.right_now)}</p>
        </SettingsCard>
      )}

      {current.gets_briefs && (
        <p className="text-[15px] text-fg-muted">
          Your morning brief and end-of-day digest still come at their usual times. They&apos;re under{" "}
          <Link href="/settings/executive" className="font-medium text-accent underline-offset-2 hover:underline">
            Your Executive
          </Link>
          .
        </p>
      )}
    </>
  );
}
