"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  getDelegation,
  getVoiceProfile,
  learnVoiceProfile,
  refreshVoiceSignature,
  resetVoiceProfile,
  setDelegationEnabled,
  updateVoiceProfile,
  type DelegationSettings,
  type VoiceProfile,
} from "@/lib/api";

// Settings → Act as me: let the Executive draft email AS you, in your own
// Gmail Drafts, when you ask it to — it never sends. Backed by GET/PUT
// /delegation and /delegation/voice. Hidden for anyone who can't have it yet
// (only the owner can) and on a backend without it.

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

  if (state === "hidden" || state === "loading") return null;
  if (state === "error" || !settings) {
    return (
      <section className="mt-3 rounded-xl border border-line bg-surface-elevated p-4">
        <h2 className="text-sm font-medium text-fg">Act as me</h2>
        <p className="mt-1 text-xs text-fg-subtle">Couldn&apos;t load this setting.</p>
      </section>
    );
  }

  const connected = settings.gmail.status === "connected";
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

  return (
    <section className="mt-3 rounded-xl border border-line bg-surface-elevated p-4">
      <h2 className="text-sm font-medium text-fg">Act as me</h2>
      <p className="mt-1 text-xs text-fg-muted leading-relaxed">
        Let the Executive write email as you, in your own voice. When you ask it to reply to or
        write an email as you, it saves a draft in your own Gmail for you to review and send — it
        never sends anything. Everything else it writes stays in its own name.
      </p>

      <div className="mt-4 space-y-5 max-w-md">
        {/* Your Gmail */}
        <div>
          <div className="text-xs font-medium text-fg">Your Gmail</div>
          <p className="text-xs text-fg-muted mt-0.5 leading-relaxed">
            {connected ? `Connected to ${settings.gmail.email}.` : settings.gmail.message}
          </p>
          {!connected && settings.gmail.status === "not_configured" && (
            <div className="mt-2">
              <p className="text-xs text-fg-muted leading-relaxed">
                On a computer with a browser, with the Executive&apos;s Google OAuth client exported, run
                this and sign in as yourself, then put the file it writes where the API reads it
                (see the Act as me section of .env.example):
              </p>
              <pre className="mt-1.5 whitespace-pre-wrap break-all rounded-md border border-line bg-surface px-2 py-1.5 text-[11px] text-fg">
                {settings.gmail.connect_command}
              </pre>
            </div>
          )}
          {!connected && (
            <button
              type="button"
              onClick={() => void recheck()}
              disabled={busy}
              className="mt-2 text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
            >
              Check again
            </button>
          )}
        </div>

        {/* The switch */}
        <div>
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-xs font-medium text-fg" id="act-as-me-label">
                Write drafts as me
              </div>
              <p className="text-xs text-fg-muted mt-0.5 leading-relaxed">
                {on
                  ? "On: ask it in chat — “reply to Dana as me: yes to the 5th” — and the draft waits in your Gmail Drafts."
                  : connected
                    ? "Off: the Executive only ever writes as itself."
                    : "Connect your Gmail first."}
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={on}
              aria-labelledby="act-as-me-label"
              disabled={busy || (!on && !connected)}
              onClick={() => void toggle()}
              className={`relative mt-0.5 inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed ${
                on ? "bg-indigo-500" : "bg-surface-overlay border border-line"
              }`}
            >
              <span
                aria-hidden="true"
                className={`inline-block h-4 w-4 rounded-full bg-white shadow transition-transform ${
                  on ? "translate-x-4" : "translate-x-0.5"
                }`}
              />
            </button>
          </div>
          {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
        </div>

        <VoiceSection connected={connected} />
      </div>
    </section>
  );
}

// "How I write": learned from your sent mail, editable, lockable.
function VoiceSection({ connected }: { connected: boolean }) {
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
    return <p className="text-xs text-fg-subtle">Couldn&apos;t load how you write.</p>;
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
  const linkButton = "text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50";

  return (
    <div>
      <div className="text-xs font-medium text-fg">How I write</div>
      <p className="text-xs text-fg-muted mt-0.5 leading-relaxed">
        {learned
          ? `Learned from ${profile.sample_count} of your sent emails${profile.locked ? " — locked, so it won't be relearned" : ""}. Drafts follow it; edit anything that isn't you.`
          : "Not learned yet. It reads your recent sent mail once, keeps only what you wrote, and describes your style — you can edit or lock it."}
      </p>

      {!profile.locked && (
        <button
          type="button"
          disabled={busy || !connected}
          onClick={() => void run(learnVoiceProfile)}
          className="mt-2 rounded-md border border-line px-2.5 py-1 text-xs text-fg hover:bg-surface-overlay disabled:opacity-50"
        >
          {busy ? "Working…" : learned ? "Learn again from my sent mail" : "Learn from my sent mail"}
        </button>
      )}

      {learned && (
        <div className="mt-3 space-y-4">
          <div className="flex gap-3">
            <label className="text-xs text-fg-muted">
              Length
              <select
                value={length}
                onChange={(e) => setLength(e.target.value)}
                className="ml-1.5 rounded border border-line bg-surface px-1 py-0.5 text-xs text-fg"
              >
                <option value="">—</option>
                {LENGTHS.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-fg-muted">
              Tone
              <select
                value={formality}
                onChange={(e) => setFormality(e.target.value)}
                className="ml-1.5 rounded border border-line bg-surface px-1 py-0.5 text-xs text-fg"
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
            <legend className="text-xs text-fg-muted">Greeting</legend>
            <p className="text-[11px] text-fg-subtle leading-relaxed">
              {"{first}"} becomes their first name. Leave one empty to let each draft choose.
            </p>
            <div className="mt-1.5 space-y-1.5">
              {AUDIENCES.map((audience) => (
                <label key={audience} className="flex items-center gap-2 text-xs text-fg-muted">
                  <span className="w-28 flex-shrink-0">{AUDIENCE_LABEL[audience]}</span>
                  <input
                    type="text"
                    value={greetings[audience] ?? ""}
                    onChange={(e) => setGreetings((g) => ({ ...g, [audience]: e.target.value }))}
                    placeholder="Not set"
                    maxLength={GREETING_MAX_CHARS}
                    className="min-w-0 flex-1 rounded border border-line bg-surface px-2 py-1 text-xs text-fg"
                  />
                </label>
              ))}
            </div>
          </fieldset>

          <label className="block text-xs text-fg-muted">
            Sign-off
            <GrowingTextarea value={signOff} onChange={setSignOff} minRows={2} />
          </label>
          <label className="block text-xs text-fg-muted">
            Habits (one per line)
            <GrowingTextarea value={habits} onChange={setHabits} minRows={3} />
          </label>
          <label className="block text-xs text-fg-muted">
            Never (one per line)
            <GrowingTextarea value={avoid} onChange={setAvoid} minRows={2} />
          </label>

          <div className="text-xs text-fg-muted">
            <div>Signature</div>
            {profile.signature ? (
              <>
                <p className="mt-0.5 leading-relaxed">
                  Added to the end of every draft, from your Gmail settings.
                </p>
                <div className="mt-1 whitespace-pre-wrap break-words border-l-2 border-line pl-2 leading-relaxed text-fg">
                  {profile.signature}
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3">
                  <button
                    type="button"
                    disabled={busy || !connected}
                    onClick={takeGmailSignature}
                    className={linkButton}
                  >
                    Refresh from Gmail
                  </button>
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
                <p className="mt-0.5 leading-relaxed">No signature is added to drafts.</p>
                <button
                  type="button"
                  disabled={busy || !connected}
                  onClick={takeGmailSignature}
                  className={`mt-1 ${linkButton}`}
                >
                  Add my Gmail signature
                </button>
              </>
            )}
          </div>

          {profile.exemplars.length > 0 && (
            <div className="text-xs text-fg-muted">
              <div>Examples of your writing</div>
              <p className="mt-0.5 leading-relaxed">
                Short passages from your sent mail that set the tone. Drafts never reuse what they say.
              </p>
              <ul className="mt-1 space-y-1.5">
                {profile.exemplars.map((example, i) => (
                  <li
                    key={i}
                    className="whitespace-pre-wrap break-words border-l-2 border-line pl-2 leading-relaxed text-fg"
                  >
                    {example}
                  </li>
                ))}
              </ul>
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(() => updateVoiceProfile({ clear_exemplars: true }), true)}
                className={`mt-1 ${linkButton}`}
              >
                Remove examples
              </button>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
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
              className="rounded-md bg-indigo-500 px-2.5 py-1 text-xs text-white hover:bg-indigo-400 disabled:opacity-50"
            >
              Save changes
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => updateVoiceProfile({ locked: !profile.locked }), true)}
              className="rounded-md border border-line px-2.5 py-1 text-xs text-fg hover:bg-surface-overlay disabled:opacity-50"
            >
              {profile.locked ? "Unlock" : "Lock"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(resetVoiceProfile)}
              className="rounded-md px-2.5 py-1 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
            >
              Reset
            </button>
          </div>
        </div>
      )}
      {notice && <p className="mt-1 text-xs text-fg-muted">{notice}</p>}
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
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
      className="mt-1 block w-full resize-none overflow-hidden rounded border border-line bg-surface px-2 py-1 text-xs leading-relaxed text-fg"
    />
  );
}
