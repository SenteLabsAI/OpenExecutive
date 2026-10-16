"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

import { FIRST, MAILS, OPTIONS, TAB_NAMES, beats, sceneFor, sceneKey, type Step, type Tab } from "@/lib/whoSends";

// Delegate → What it may do: "Who writes and who sends". Pick a tab and an
// option and its scene plays as numbered steps that stay on screen (or, for
// Handle it for me, the same five emails marked sent or waiting), under
// three answers: who writes it, whose name is on it, who presses Send
// (lib/whoSends.ts). It changes no setting. Hide is remembered in this
// browser.

const HIDDEN_KEY = "oe.whoSends.hidden";
const STEP_MS = 1700;
const MAIL_MS = 900;

// The feature colours, as on FeatureName.
const TONE: Record<Tab, { line: string; lit: string; ring: string; dot: string }> = {
  act: {
    line: "border-indigo-500 text-fg dark:border-indigo-300",
    lit: "bg-indigo-600 border-indigo-600 text-white dark:bg-indigo-300 dark:border-indigo-300 dark:text-indigo-950",
    ring: "border-indigo-400 ring-4 ring-indigo-500/15",
    dot: "bg-indigo-600 text-white dark:bg-indigo-300 dark:text-indigo-950",
  },
  handle: {
    line: "border-emerald-500 text-fg dark:border-emerald-300",
    lit: "bg-emerald-600 border-emerald-600 text-white dark:bg-emerald-300 dark:border-emerald-300 dark:text-emerald-950",
    ring: "border-emerald-400 ring-4 ring-emerald-500/15",
    dot: "bg-emerald-600 text-white dark:bg-emerald-300 dark:text-emerald-950",
  },
  lead: {
    line: "border-orange-500 text-fg dark:border-orange-300",
    lit: "bg-orange-600 border-orange-600 text-white dark:bg-orange-300 dark:border-orange-300 dark:text-orange-950",
    ring: "border-orange-400 ring-4 ring-orange-500/15",
    dot: "bg-orange-600 text-white dark:bg-orange-300 dark:text-orange-950",
  },
};

const ACTOR: Record<Step["who"], string> = {
  you: "text-fg",
  exec: "text-indigo-700 dark:text-indigo-300",
  them: "text-fg-muted",
};

const MAIL_AS = {
  you: "border-indigo-300 bg-indigo-50 dark:border-indigo-400/40 dark:bg-indigo-500/15",
  exec: "border-orange-300 bg-orange-50 dark:border-orange-400/40 dark:bg-orange-500/15",
};

// Hide, kept in this browser. Read through useSyncExternalStore so the
// server render (shown) and the first client render agree.
const hiddenListeners = new Set<() => void>();
let sessionHidden = false;

function readHidden(): boolean {
  try {
    return window.localStorage.getItem(HIDDEN_KEY) === "1";
  } catch {
    return false;
  }
}

function writeHidden(hidden: boolean): void {
  try {
    if (hidden) window.localStorage.setItem(HIDDEN_KEY, "1");
    else window.localStorage.removeItem(HIDDEN_KEY);
  } catch {
    /* storage unavailable: Hide lasts until the page reloads */
    sessionHidden = hidden;
  }
  hiddenListeners.forEach((fn) => fn());
}

function useHidden(): boolean {
  return useSyncExternalStore(
    (fn) => {
      hiddenListeners.add(fn);
      return () => hiddenListeners.delete(fn);
    },
    () => readHidden() || sessionHidden,
    () => false,
  );
}

const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function useReducedMotion(): boolean {
  return useSyncExternalStore(
    (fn) => {
      const q = window.matchMedia(MOTION_QUERY);
      q.addEventListener("change", fn);
      return () => q.removeEventListener("change", fn);
    },
    () => window.matchMedia(MOTION_QUERY).matches,
    () => false,
  );
}

function Steps({ steps, shown, tab }: { steps: Step[]; shown: number; tab: Tab }) {
  return (
    <ol
      className="grid gap-5 @min-[640px]:gap-6"
      style={{ gridTemplateColumns: `repeat(var(--cols), minmax(0, 1fr))` }}
    >
      {steps.map((s, i) => {
        const seen = i < shown;
        const now = i === shown - 1;
        return (
          <li
            key={i}
            className={`relative rounded-xl border p-3 transition-all duration-500 ${
              seen ? "opacity-100" : "translate-y-1.5 opacity-15"
            } ${now ? TONE[tab].ring : "border-line"}`}
          >
            <div className="flex items-center gap-2">
              <span
                className={`grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-bold ${
                  now ? TONE[tab].dot : "bg-surface-hover text-fg"
                }`}
              >
                {i + 1}
              </span>
              <span className={`text-xs font-bold uppercase tracking-wide ${ACTOR[s.who]}`}>{s.name}</span>
            </div>
            <p className="mt-1.5 text-sm leading-snug text-fg">{s.does}</p>
            {i < steps.length - 1 && (
              <span
                aria-hidden
                className="absolute -bottom-5 left-1/2 -translate-x-1/2 text-fg-subtle @min-[640px]:-right-5 @min-[640px]:bottom-auto @min-[640px]:left-auto @min-[640px]:top-1/2 @min-[640px]:-translate-y-1/2 @min-[640px]:translate-x-0"
              >
                <span className="@min-[640px]:hidden">↓</span>
                <span className="hidden @min-[640px]:inline">→</span>
              </span>
            )}
            {s.mail && (
              <div className={`mt-2 rounded-lg border px-2.5 py-1.5 text-[13px] text-fg ${s.mail.as ? MAIL_AS[s.mail.as] : "border-line bg-surface-overlay"}`}>
                <span className="block text-[11px] text-fg-muted">{s.mail.from}</span>
                {s.mail.text}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Sorted({ sends, shown }: { sends: number; shown: number }) {
  return (
    <>
      <ul className="grid gap-2">
        {MAILS.map((m, i) => {
          const seen = i < shown;
          const sent = i < sends;
          return (
            <li
              key={m.from}
              className={`flex items-center justify-between gap-3 rounded-xl border border-line px-3 py-2 transition-opacity duration-500 ${
                seen ? "opacity-100" : "opacity-15"
              }`}
            >
              <div className="min-w-0">
                <span className="block text-xs text-fg-muted">{m.from}</span>
                <span className="text-sm text-fg">{m.text}</span>
              </div>
              <span
                className={`shrink-0 rounded-full px-2.5 py-1 text-right text-[13px] font-semibold transition-all delay-300 duration-300 ${
                  seen ? "opacity-100" : "translate-x-2 opacity-0"
                } ${
                  sent
                    ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300"
                    : "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300"
                }`}
              >
                {sent ? "Sent as you" : "Waits for you"}
                <span className="block text-[11px] font-normal">{m.why}</span>
              </span>
            </li>
          );
        })}
      </ul>
      <p className={`mt-3 flex gap-4 text-sm text-fg-muted transition-opacity duration-500 ${shown >= MAILS.length ? "opacity-100" : "opacity-0"}`}>
        <span>
          Sent on its own: <b className="text-fg">{sends}</b>
        </span>
        <span>
          Waits on Home: <b className="text-fg">{MAILS.length - sends}</b>
        </span>
      </p>
    </>
  );
}

export default function WhoSendsCard() {
  const hidden = useHidden();
  const reduced = useReducedMotion();
  const [tab, setTab] = useState<Tab>("act");
  const [option, setOption] = useState(FIRST.act);
  const [shown, setShown] = useState(1);

  const scene = sceneFor(sceneKey(tab, option));
  const total = beats(scene);
  const visible = reduced ? total : shown;

  useEffect(() => {
    if (hidden || reduced || shown >= total) return;
    const id = setTimeout(() => setShown(shown + 1), scene.steps ? STEP_MS : MAIL_MS);
    return () => clearTimeout(id);
  }, [hidden, reduced, shown, total, scene.steps]);

  const play = (nextTab: Tab, nextOption: string) => {
    setTab(nextTab);
    setOption(nextOption);
    setShown(1);
  };

  if (hidden) {
    return (
      <button
        type="button"
        onClick={() => writeHidden(false)}
        className="mt-6 text-sm font-medium text-accent underline-offset-2 hover:underline"
      >
        Show who writes and who sends
      </button>
    );
  }

  const answers = [
    ["Who writes it", scene.answers.writes],
    ["Whose name is on it", scene.answers.name],
    ["Who presses Send", scene.answers.sends],
  ];

  return (
    <section aria-labelledby="who-sends-title" className="@container mt-6 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="who-sends-title" className="text-base font-semibold text-fg">
            Who writes and who sends
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-fg-muted">Pick a tab and an option to see what happens to your mail.</p>
        </div>
        <button
          type="button"
          onClick={() => writeHidden(true)}
          className="whitespace-nowrap text-sm font-medium text-fg-muted hover:text-fg"
        >
          Hide
        </button>
      </div>

      <div className="mt-3 flex gap-1 overflow-x-auto border-b border-line">
        {(Object.keys(TAB_NAMES) as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            aria-pressed={t === tab}
            onClick={() => play(t, FIRST[t])}
            className={`-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-semibold transition-colors ${
              t === tab ? TONE[t].line : "border-transparent text-fg-muted hover:text-fg"
            }`}
          >
            {TAB_NAMES[t]}
          </button>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {OPTIONS[tab].map((o) => {
          const lit = o.value === option;
          return (
            <button
              key={o.value}
              type="button"
              aria-pressed={lit}
              onClick={() => play(tab, o.value)}
              className={`min-w-[5.5rem] flex-1 basis-[30%] rounded-lg border px-2 py-2 text-center text-[13px] leading-tight transition-colors @min-[640px]:basis-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg ${
                lit ? `${TONE[tab].lit} font-semibold` : "border-line text-fg-muted hover:border-line-strong hover:text-fg"
              }`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-fg-subtle">This is a demo. Tapping here doesn&apos;t change your settings.</p>

      <dl className="mt-3 grid grid-cols-3 gap-2">
        {answers.map(([label, value]) => (
          <div key={label} className="rounded-xl border border-line bg-surface-base px-2.5 py-2 @min-[640px]:px-3">
            <dt className="text-[10px] uppercase tracking-wider text-fg-subtle @min-[640px]:text-[11px]">{label}</dt>
            <dd className="mt-0.5 text-sm font-semibold leading-snug text-fg @min-[640px]:text-[15px]">{value}</dd>
          </div>
        ))}
      </dl>

      <div className="mt-3 rounded-xl border border-line bg-surface-base p-3 sm:p-4">
        <p className="mb-3 text-[15px] leading-relaxed text-fg" aria-live="polite">
          {scene.line}
        </p>
        <div
          className="[--cols:1] @min-[640px]:[--cols:var(--n)]"
          style={{ ["--n" as string]: String(scene.steps?.length ?? 1) }}
        >
          {scene.steps ? (
            <Steps steps={scene.steps} shown={visible} tab={tab} />
          ) : (
            <Sorted sends={scene.sends ?? 0} shown={visible} />
          )}
        </div>
      </div>

      {!reduced && (
        <button
          type="button"
          onClick={() => setShown(1)}
          className="mt-3 rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface-hover"
        >
          Play again
        </button>
      )}
    </section>
  );
}
