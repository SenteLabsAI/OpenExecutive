"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import AdvancedFold from "@/components/settings/AdvancedFold";
import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import Button from "@/components/ui/Button";
import {
  checkInboxNow,
  getDelegation,
  getHandledReplies,
  getVoiceProfile,
  learnVoiceProfile,
  refreshVoiceSignature,
  resetVoiceProfile,
  setDelegationEnabled,
  setDelegationTeam,
  setHandleIt,
  setInboxWatch,
  updateVoiceProfile,
  type DelegationSettings,
  type DelegationTeam,
  type HandledReply,
  type HandleIt,
  type HandleItLevel,
  type InboxWatch,
  type VoiceProfile,
} from "@/lib/api";
import { formatAgo } from "@/lib/setupStatus";

// Settings → Act as me: let the Executive draft email AS you, in your own
// Gmail Drafts, when you ask it to — and, with Draft replies to my inbox on,
// for mail that needs you, which it sends when you tap Send on Today (or on
// its own, under Handle it for me).
// Backed by GET/PUT /delegation, /delegation/inbox and /delegation/voice.
// Not offered to anyone who can't have it (the owner can, and team members
// once the owner lets them: PUT /delegation/team) or on a backend without
// it: the hub then shows no tile, and this page says so.
//
// The page body: the mailbox, Write drafts as me and Draft replies to my
// inbox up front; How I write and Let team members use it under Advanced.

export const ACT_AS_ME_INTRO =
  "Let the Executive write email as you, in your own voice. When you ask it to reply to or write an email as you, it saves a draft in your own mailbox (Gmail or Outlook) for you to review and send. Nothing goes out as you unless you send it, from your mailbox or with Send on a reply waiting on Today. Everything else it writes stays in its own name.";

const LENGTHS = ["short", "medium", "long"] as const;
const FORMALITIES = ["casual", "neutral", "formal"] as const;
// The audiences a greeting is learned for (delegation/voice.py AUDIENCES) and
// its per-line limit (GREETING_MAX_CHARS).
const AUDIENCES = ["team", "contact", "other"] as const;
const AUDIENCE_LABEL: Record<string, string> = {
  team: "To your team",
  contact: "To your contacts",
  other: "To anyone else",
};
const GREETING_MAX_CHARS = 60;
// While Check now runs, how often the card looks again, and for how long.
const CHECK_POLL_MS = 3000;
const MAX_CHECK_POLLS = 40;

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}

// The greetings as the form edits them: one per audience, "" for none.
function greetingFields(p: VoiceProfile): Record<string, string> {
  return Object.fromEntries(AUDIENCES.map((a) => [a, p.greetings[a] ?? ""]));
}

export default function ActAsMeCard() {
  const [settings, setSettings] = useState<DelegationSettings | null>(null);
  const [state, setState] = useState<"loading" | "hidden" | "ready" | "error">("loading");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await getDelegation(signal);
      if (!next) {
        setState("hidden");
        return;
      }
      setSettings(next);
      setState("ready");
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      setState("error");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // While an inbox check runs, look again every few seconds until it's done.
  // A failed look keeps the card as it is and tries again; after
  // MAX_CHECK_POLLS it stops waiting and offers Check now again.
  const checking = settings?.inbox?.checking ?? false;
  const polls = useRef(0);
  const [pollTick, setPollTick] = useState(0);
  const [stoppedWaiting, setStoppedWaiting] = useState(false);
  useEffect(() => {
    if (!checking) {
      polls.current = 0;
      return;
    }
    if (polls.current >= MAX_CHECK_POLLS) {
      setStoppedWaiting(true);
      return;
    }
    const timer = setTimeout(() => {
      polls.current += 1;
      getDelegation()
        .then((next) => {
          if (next) setSettings(next);
        })
        .catch(() => { /* keep the card; the next look may work */ })
        .finally(() => setPollTick((n) => n + 1));
    }, CHECK_POLL_MS);
    return () => clearTimeout(timer);
  }, [checking, pollTick]);

  if (state === "loading") return <p className="text-[15px] text-fg-muted">Loading…</p>;
  if (state === "hidden") {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted leading-relaxed">
          Act as me isn&apos;t available to you here. The owner of this Open Executive can turn it
          on for team members.
        </p>
      </SettingsCard>
    );
  }
  if (state === "error" || !settings) {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted">Couldn&apos;t load this setting.</p>
      </SettingsCard>
    );
  }

  const connected = settings.gmail.status === "connected";
  const outlook = settings.gmail.provider === "microsoft";
  const mailbox = outlook ? "Outlook" : "Gmail";
  const on = settings.enabled;

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      setSettings(await setDelegationEnabled(!on));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  const recheck = async () => {
    setBusy(true);
    setError(null);
    await load();
    setBusy(false);
  };

  const pre =
    "mt-2 whitespace-pre-wrap break-all rounded-xl border border-line bg-surface px-3 py-2 text-xs text-fg font-mono";
  return (
    <>
      <SettingsCard
        title={connected ? `Your ${mailbox}` : "Your mailbox"}
        description={connected ? `Connected to ${settings.gmail.email}.` : settings.gmail.message}
        action={
          connected ? (
            <span className="inline-flex items-center gap-2 text-sm font-medium text-fg">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" aria-hidden="true" />
              Connected
            </span>
          ) : undefined
        }
      >
        {!connected && (
          <div className="space-y-3">
            {settings.gmail.status === "not_configured" && (
              <div>
                <p className="text-sm text-fg-muted leading-relaxed">
                  For Gmail, on a computer with a browser, with the Executive&apos;s Google OAuth
                  client exported, run this and sign in as yourself, then put the file it writes
                  where the API reads it (see the Act as me section of .env.example):
                </p>
                <pre className={pre}>{settings.gmail.connect_command}</pre>
                {settings.gmail.outlook_connect_command && (
                  <>
                    <p className="mt-3 text-sm text-fg-muted leading-relaxed">
                      For Outlook, with the Executive&apos;s Microsoft 365 app exported, run this
                      instead and sign in as yourself with the code it prints:
                    </p>
                    <pre className={pre}>{settings.gmail.outlook_connect_command}</pre>
                  </>
                )}
              </div>
            )}
            <Button variant="primary" onClick={() => void recheck()} disabled={busy}>
              {busy ? "Checking…" : "Check again"}
            </Button>
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        title="Write drafts as me"
        titleId="act-as-me-label"
        description={
          on
            ? `On: ask it in chat — “reply to Dana as me: yes to the 5th” — and the draft waits in your ${mailbox} Drafts.`
            : connected
              ? "Off: the Executive only ever writes as itself."
              : "Connect your mailbox first."
        }
        action={
          <Switch
            checked={on}
            onChange={() => void toggle()}
            disabled={busy || (!on && !connected)}
            labelledBy="act-as-me-label"
          />
        }
      >
        {error && <p className="text-sm text-red-500">{error}</p>}
      </SettingsCard>

      {settings.inbox && (
        <InboxSection
          inbox={stoppedWaiting ? { ...settings.inbox, checking: false } : settings.inbox}
          actAsMeOn={on}
          handleItOn={!!settings.handle_it?.enabled}
          onSettings={setSettings}
          onInbox={(inbox) => {
            setStoppedWaiting(false);
            polls.current = 0;
            setSettings((prev) => (prev ? { ...prev, inbox } : prev));
          }}
        />
      )}

      {settings.handle_it && settings.inbox && (
        <HandleItSection
          handleIt={settings.handle_it}
          inboxOn={settings.inbox.enabled}
          onSettings={setSettings}
        />
      )}

      <AdvancedFold
        id="act-as-me-advanced"
        summary={settings.team ? "How I write · Let team members use it" : "How I write"}
      >
        <VoiceSection connected={connected} outlook={outlook} />
        {settings.team && <TeamSection team={settings.team} onSettings={setSettings} />}
      </AdvancedFold>
    </>
  );
}

// The owner's "Let team members use Act as me" (PUT /delegation/team), shown
// only to the owner and only where the install allows it. Each member
// connects their own Gmail and turns it on for themselves; the owner sees
// counts only, never their mail, drafts or reply cards.
function TeamSection({
  team,
  onSettings,
}: {
  team: DelegationTeam;
  onSettings: (next: DelegationSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await setDelegationTeam(!team.enabled));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsCard
      title="Let team members use it"
      titleId="act-as-me-team-label"
      description={
        team.enabled
          ? "On: each team member can connect their own Gmail or Outlook and turn it on for themselves. Their mail, drafts and replies stay theirs alone. You see only who uses it and how much."
          : "Off: only you can use Act as me."
      }
      action={
        <Switch
          checked={team.enabled}
          onChange={() => void toggle()}
          disabled={busy}
          labelledBy="act-as-me-team-label"
        />
      }
    >
      {team.enabled && team.members.length > 0 && (
        <ul className="divide-y divide-line">
          {team.members.map((m) => (
            <li key={m.person_id} className="py-2 text-sm text-fg-muted">
              <span className="font-medium text-fg">{m.name}</span>
              {`: ${m.drafts_30d} ${m.drafts_30d === 1 ? "draft" : "drafts"}, ${m.sent_30d} sent in the last 30 days`}
              {m.inbox ? ". Drafts replies to their inbox." : "."}
            </li>
          ))}
        </ul>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

// "Draft replies to my inbox" (PUT /delegation/inbox; Check now is POST
// /delegation/inbox/check). Needs Act as me on; the replies wait on Today.
function InboxSection({
  inbox,
  actAsMeOn,
  handleItOn,
  onSettings,
  onInbox,
}: {
  inbox: InboxWatch;
  actAsMeOn: boolean;
  handleItOn: boolean;
  onSettings: (next: DelegationSettings) => void;
  onInbox: (next: InboxWatch) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const on = inbox.enabled;

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      onSettings(await setInboxWatch(!on));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  const checkNow = async () => {
    setBusy(true);
    setError(null);
    try {
      onInbox(await checkInboxNow());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't check your inbox.");
    } finally {
      setBusy(false);
    }
  };

  const last = inbox.last_poll_at && !inbox.checking ? ` Last checked ${formatAgo(inbox.last_poll_at)}.` : "";
  return (
    <SettingsCard
      title="Draft replies to my inbox"
      titleId="act-as-me-inbox-label"
      description={
        on
          ? handleItOn
            ? "When mail comes in that needs you, it writes a first reply in your Drafts. Handle it for me, below, sends the simple ones; the rest wait on Today, where you send, edit or dismiss them."
            : "When mail comes in that needs you, it writes a first reply in your Drafts and puts it on Today, where you send it, edit it in your mailbox or dismiss it. Nothing is sent until you tap Send."
          : actAsMeOn
            ? "Off: it only drafts when you ask it to in chat."
            : "Turn on Write drafts as me first."
      }
      action={
        <Switch
          checked={on}
          onChange={() => void toggle()}
          disabled={busy || (!on && !actAsMeOn)}
          labelledBy="act-as-me-inbox-label"
        />
      }
    >
      {on && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="text-sm text-fg-muted">
            {inbox.message}
            {last}
          </span>
          <Button size="sm" onClick={() => void checkNow()} disabled={busy || inbox.checking}>
            {inbox.checking ? "Checking…" : "Check now"}
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

// Handle it for me (PUT /delegation/handle-it): replies the inbox watcher
// sends on its own. Plain code decides each one (delegation/handle_it.py);
// anything it won't send waits on Today as before. Below the switch, what it
// sent in the last week (GET /delegation/handled).
const HANDLE_IT_KINDS: { kind: string; label: string }[] = [
  { kind: "reply_known", label: "People you know" },
  { kind: "reply_stranger", label: "People you haven't written to" },
];

function HandleItSection({
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

  useEffect(() => {
    if (!on) return;
    const controller = new AbortController();
    getHandledReplies(controller.signal)
      .then(setHandled)
      .catch(() => setHandled(null));
    return () => controller.abort();
  }, [on, handleIt.sent_today]);

  const save = async (update: { enabled?: boolean; levels?: Record<string, HandleItLevel> }) => {
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
      titleId="act-as-me-handle-it-label"
      description={
        !handleIt.available
          ? "Needs signed sign-ins on this server before it can send anything as you."
          : on
            ? "It sends replies on its own when they're simple and safe, and tells you here. Anything about money, contracts, legal, hiring or the press, anything with a link or an amount, and anything going to someone new still waits for you on Today."
            : inboxOn
              ? "Off: every reply waits for you to tap Send."
              : "Turn on Draft replies to my inbox first."
      }
      action={
        <Switch
          checked={on}
          onChange={() => void save({ enabled: !on })}
          disabled={busy || (!on && (!inboxOn || !handleIt.available))}
          labelledBy="act-as-me-handle-it-label"
        />
      }
    >
      {on && (
        <div className="flex flex-col gap-3">
          {HANDLE_IT_KINDS.map(({ kind, label }) => {
            const id = `handle-it-${kind}`;
            return (
              <div key={kind} className="flex flex-wrap items-center justify-between gap-2">
                <label htmlFor={id} className="text-sm">
                  {label}
                </label>
                <select
                  id={id}
                  className="rounded-md border border-border bg-bg px-2 py-1 text-sm"
                  value={handleIt.levels[kind] === "handle" ? "handle" : "ask"}
                  disabled={busy}
                  onChange={(e) => void save({ levels: { [kind]: e.target.value as HandleItLevel } })}
                >
                  <option value="handle">Handle it</option>
                  <option value="ask">Ask me</option>
                </select>
              </div>
            );
          })}
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

// "How I write": learned from your sent mail, editable, lockable. The
// summary line and the Learn button are always in view; the fields sit
// behind "Edit how I write", and stay open while an edit is unsaved.
function VoiceSection({ connected, outlook = false }: { connected: boolean; outlook?: boolean }) {
  const [editing, setEditing] = useState(false);
  const [profile, setProfile] = useState<VoiceProfile | null>(null);
  const [greetings, setGreetings] = useState<Record<string, string>>({});
  const [habits, setHabits] = useState("");
  const [avoid, setAvoid] = useState("");
  const [signOff, setSignOff] = useState("");
  const [length, setLength] = useState("");
  const [formality, setFormality] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  const adopt = useCallback((p: VoiceProfile) => {
    setProfile(p);
    setGreetings(greetingFields(p));
    setHabits(p.habits.join("\n"));
    setAvoid(p.avoid.join("\n"));
    setSignOff(p.sign_off);
    setLength(p.length);
    setFormality(p.formality);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getVoiceProfile(controller.signal)
      .then(adopt)
      .catch((err) => {
        if ((err as Error)?.name === "AbortError") return;
        setLoadFailed(true);
      });
    return () => controller.abort();
  }, [adopt]);

  if (loadFailed) {
    return (
      <SettingsCard title="How I write">
        <p className="text-sm text-fg-muted">Couldn&apos;t load how you write.</p>
      </SettingsCard>
    );
  }
  if (!profile) return null;

  // keepEdits (lock, signature, examples): a field you've changed and not
  // saved keeps your text; every other field takes the new value, which may
  // come from a change made elsewhere.
  const run = async (action: () => Promise<VoiceProfile>, keepEdits = false) => {
    const before = profile;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await action();
      if (!keepEdits) {
        adopt(next);
        return;
      }
      const keep = (saved: string, fresh: string) => (typed: string) => (typed === saved ? fresh : typed);
      setProfile(next);
      setHabits(keep(before.habits.join("\n"), next.habits.join("\n")));
      setAvoid(keep(before.avoid.join("\n"), next.avoid.join("\n")));
      setSignOff(keep(before.sign_off, next.sign_off));
      setLength(keep(before.length, next.length));
      setFormality(keep(before.formality, next.formality));
      const [was, now] = [greetingFields(before), greetingFields(next)];
      setGreetings((typed) =>
        Object.fromEntries(AUDIENCES.map((a) => [a, keep(was[a], now[a])(typed[a] ?? "")])),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  };

  const takeGmailSignature = () =>
    void run(async () => {
      const next = await refreshVoiceSignature();
      if (!next.signature) {
        setNotice(
          profile.signature
            ? "Your Gmail settings have no signature now, so none is added."
            : "Your Gmail settings have no signature to add.",
        );
      }
      return next;
    }, true);

  const learned = profile.learned_at !== null;
  const savedGreetings = greetingFields(profile);
  const dirty =
    AUDIENCES.some((a) => greetings[a] !== savedGreetings[a]) ||
    habits !== profile.habits.join("\n") ||
    avoid !== profile.avoid.join("\n") ||
    signOff !== profile.sign_off ||
    length !== profile.length ||
    formality !== profile.formality;
  const linkButton =
    "min-h-touch text-sm font-medium text-accent hover:underline disabled:opacity-50 disabled:no-underline";
  const field =
    "rounded-xl border border-line bg-surface px-3 text-[15px] text-fg focus:outline-none focus:border-line-strong";
  const showForm = editing || dirty;

  return (
    <SettingsCard
      title="How I write"
      description={
        learned
          ? `Learned from ${profile.sample_count} of your sent emails${profile.locked ? " — locked, so it won't be relearned" : ""}. Drafts follow it; edit anything that isn't you.`
          : "Not learned yet. It reads your recent sent mail once, keeps only what you wrote, and describes your style — you can edit or lock it."
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        {!profile.locked && (
          <Button
            variant={learned ? "secondary" : "primary"}
            size="sm"
            disabled={busy || !connected}
            onClick={() => void run(learnVoiceProfile)}
          >
            {busy ? "Working…" : learned ? "Learn again from my sent mail" : "Learn from my sent mail"}
          </Button>
        )}
        {learned && !dirty && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setEditing((v) => !v)}
            aria-expanded={showForm}
            aria-controls="voice-editor"
          >
            {showForm ? "Done editing" : "Edit how I write"}
            <Icon
              name="chevron-right"
              size="w-4 h-4"
              className={`transition-transform ${showForm ? "rotate-90" : ""}`}
            />
          </Button>
        )}
      </div>

      {learned && showForm && (
        <div id="voice-editor" className="mt-5 space-y-5">
          <div className="flex flex-wrap gap-4">
            <label className="flex items-center gap-2 text-sm text-fg-muted">
              Length
              <select value={length} onChange={(e) => setLength(e.target.value)} className={`${field} h-10`}>
                <option value="">—</option>
                {LENGTHS.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-sm text-fg-muted">
              Tone
              <select
                value={formality}
                onChange={(e) => setFormality(e.target.value)}
                className={`${field} h-10`}
              >
                <option value="">—</option>
                {FORMALITIES.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <fieldset>
            <legend className="text-sm font-medium text-fg">Greeting</legend>
            <p className="text-[13px] text-fg-subtle leading-relaxed">
              {"{first}"} becomes their first name. Leave one empty to let each draft choose.
            </p>
            <div className="mt-2 space-y-2">
              {AUDIENCES.map((audience) => (
                <label
                  key={audience}
                  className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-3 text-sm text-fg-muted"
                >
                  <span className="sm:w-36 flex-shrink-0">{AUDIENCE_LABEL[audience]}</span>
                  <input
                    type="text"
                    value={greetings[audience] ?? ""}
                    onChange={(e) => setGreetings((g) => ({ ...g, [audience]: e.target.value }))}
                    placeholder="Not set"
                    maxLength={GREETING_MAX_CHARS}
                    className={`${field} h-10 min-w-0 flex-1`}
                  />
                </label>
              ))}
            </div>
          </fieldset>

          <label className="block text-sm font-medium text-fg">
            Sign-off
            <GrowingTextarea value={signOff} onChange={setSignOff} minRows={2} />
          </label>
          <label className="block text-sm font-medium text-fg">
            Habits (one per line)
            <GrowingTextarea value={habits} onChange={setHabits} minRows={3} />
          </label>
          <label className="block text-sm font-medium text-fg">
            Never (one per line)
            <GrowingTextarea value={avoid} onChange={setAvoid} minRows={2} />
          </label>

          <div className="text-sm text-fg-muted">
            <div className="font-medium text-fg">Signature</div>
            {profile.signature ? (
              <>
                <p className="mt-0.5 leading-relaxed">
                  {outlook
                    ? "Added to the end of every draft. Outlook doesn't share your signature with apps, so it can't be refreshed from there."
                    : "Added to the end of every draft, from your Gmail settings."}
                </p>
                <div className="mt-2 whitespace-pre-wrap break-words border-l-2 border-line pl-3 leading-relaxed text-fg">
                  {profile.signature}
                </div>
                <div className="mt-1 flex flex-wrap gap-x-4">
                  {!outlook && (
                    <button
                      type="button"
                      disabled={busy || !connected}
                      onClick={takeGmailSignature}
                      className={linkButton}
                    >
                      Refresh from Gmail
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(() => updateVoiceProfile({ clear_signature: true }), true)}
                    className={linkButton}
                  >
                    Don&apos;t add my signature
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="mt-0.5 leading-relaxed">
                  {outlook
                    ? "No signature is added to drafts: Outlook doesn't share your signature with apps, so drafts end with your sign-off."
                    : "No signature is added to drafts."}
                </p>
                {!outlook && (
                  <button
                    type="button"
                    disabled={busy || !connected}
                    onClick={takeGmailSignature}
                    className={linkButton}
                  >
                    Add my Gmail signature
                  </button>
                )}
              </>
            )}
          </div>

          {profile.exemplars.length > 0 && (
            <div className="text-sm text-fg-muted">
              <div className="font-medium text-fg">Examples of your writing</div>
              <p className="mt-0.5 leading-relaxed">
                Short passages from your sent mail that set the tone. Drafts never reuse what they say.
              </p>
              <ul className="mt-2 space-y-2">
                {profile.exemplars.map((example, i) => (
                  <li
                    key={i}
                    className="whitespace-pre-wrap break-words border-l-2 border-line pl-3 leading-relaxed text-fg"
                  >
                    {example}
                  </li>
                ))}
              </ul>
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(() => updateVoiceProfile({ clear_exemplars: true }), true)}
                className={linkButton}
              >
                Remove examples
              </button>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
            <Button
              variant="primary"
              disabled={busy || !dirty}
              onClick={() =>
                void run(() =>
                  updateVoiceProfile({
                    greetings: Object.fromEntries(
                      AUDIENCES.map((a) => [a, (greetings[a] ?? "").trim()]).filter(([, g]) => g),
                    ),
                    habits: lines(habits),
                    avoid: lines(avoid),
                    sign_off: signOff,
                    length,
                    formality,
                  }),
                )
              }
            >
              Save changes
            </Button>
            <Button
              disabled={busy}
              onClick={() => void run(() => updateVoiceProfile({ locked: !profile.locked }), true)}
            >
              {profile.locked ? "Unlock" : "Lock"}
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => void run(resetVoiceProfile)}>
              Reset
            </Button>
          </div>
        </div>
      )}
      {notice && <p className="mt-2 text-sm text-fg-muted">{notice}</p>}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}

// A textarea as tall as its text, so every line shows without an inner
// scrollbar; it refits when the text changes (typed or loaded) and when the
// window's width does.
function GrowingTextarea({
  value,
  onChange,
  minRows,
}: {
  value: string;
  onChange: (value: string) => void;
  minRows: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fit = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    // scrollHeight leaves out the border, which border-box sizing counts.
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  }, []);
  useLayoutEffect(fit, [fit, value]);
  useEffect(() => {
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [fit]);
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={minRows}
      className="mt-1.5 block w-full resize-none overflow-hidden rounded-xl border border-line bg-surface px-3 py-2 text-[15px] font-normal leading-relaxed text-fg focus:outline-none focus:border-line-strong"
    />
  );
}
