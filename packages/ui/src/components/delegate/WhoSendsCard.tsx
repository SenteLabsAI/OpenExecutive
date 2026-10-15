"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  LEAD_OPTIONS,
  SEND_OPTIONS,
  TAB_NAMES,
  TOUR,
  WRITE_OPTIONS,
  nextInTour,
  sceneFor,
  sceneKey,
  type Frame,
  type Spot,
  type Tab,
} from "@/lib/whoSends";

// Delegate → What it may do: "Who writes and who sends", a short animation of
// mail moving between your inbox, the Executive, you and the person it goes
// to. It plays one scene per tab in turn; tapping an option plays that
// option's scene (lib/whoSends.ts). It changes no setting. Hide is
// remembered in this browser.

const HIDDEN_KEY = "oe.whoSends.hidden";
const STEP_MS = 1500;
const HOLD_MS = 1900;

// The feature colours, as on FeatureName.
const TONE: Record<Tab, { text: string; pill: string; ring: string; chip: string; lit: string }> = {
  act: {
    text: "text-indigo-700 dark:text-indigo-300",
    pill: "bg-indigo-100 dark:bg-indigo-500/15",
    ring: "border-indigo-400 ring-4 ring-indigo-500/15",
    chip: "bg-indigo-200 dark:bg-indigo-300",
    lit: "bg-indigo-600 border-indigo-600 text-white dark:bg-indigo-300 dark:border-indigo-300 dark:text-indigo-950",
  },
  handle: {
    text: "text-emerald-700 dark:text-emerald-300",
    pill: "bg-emerald-100 dark:bg-emerald-500/15",
    ring: "border-emerald-400 ring-4 ring-emerald-500/15",
    chip: "bg-emerald-200 dark:bg-emerald-300",
    lit: "bg-emerald-600 border-emerald-600 text-white dark:bg-emerald-300 dark:border-emerald-300 dark:text-emerald-950",
  },
  lead: {
    text: "text-orange-700 dark:text-orange-300",
    pill: "bg-orange-100 dark:bg-orange-500/15",
    ring: "border-orange-400 ring-4 ring-orange-500/15",
    chip: "bg-orange-200 dark:bg-orange-300",
    lit: "bg-orange-600 border-orange-600 text-white dark:bg-orange-300 dark:border-orange-300 dark:text-orange-950",
  },
};

// Where each node sits, and where mail lands next to it, in % of the stage.
const NODE_AT: Record<Spot, [number, number]> = { inbox: [15, 30], exec: [50, 30], you: [50, 76], to: [85, 30] };
const CHIP_AT: Record<Spot, [number, number]> = { inbox: [15, 11], exec: [50, 8], you: [77, 76], to: [74, 11] };

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

function Node({ spot, label, frame, tone, round }: { spot: Spot; label: string; frame: Frame; tone: Tab; round?: boolean }) {
  const [x, y] = NODE_AT[spot];
  const on = frame.on?.includes(spot);
  const dim = frame.dim?.includes(spot);
  return (
    <div
      className={`absolute -translate-x-1/2 -translate-y-1/2 grid justify-items-center gap-[.35em] text-center transition-opacity duration-500 ${dim ? "opacity-35" : ""}`}
      style={{ left: `${x}%`, top: `${y}%` }}
    >
      <div
        className={`grid place-items-center border bg-surface-overlay font-semibold text-fg transition-all duration-500 ${
          round ? "h-[6.6em] w-[6.6em] rounded-full font-display" : "h-[4.4em] w-[7.5em] rounded-[.8em]"
        } ${on ? TONE[tone].ring : "border-line"}`}
      >
        {label}
      </div>
      <div className="min-h-[1.3em] whitespace-nowrap text-[.85em] text-fg-muted">{frame.caps?.[spot] ?? ""}</div>
    </div>
  );
}

function Options<T extends string>({
  title,
  group,
  options,
  current,
  tone,
  onPick,
}: {
  title: string;
  group: "writes" | "sends" | "lead";
  options: { value: T; label: string }[];
  current: T;
  tone: Tab;
  onPick: (key: string) => void;
}) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-subtle">{title}</h3>
      <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
        {options.map((o) => {
          const lit = o.value === current;
          return (
            <button
              key={o.value}
              type="button"
              aria-pressed={lit}
              onClick={() => onPick(sceneKey(group, o.value))}
              className={`min-w-0 rounded-lg border px-1 py-2 text-center text-xs leading-tight transition-colors sm:text-[13px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg ${
                lit ? `${TONE[tone].lit} font-semibold` : "border-line text-fg-muted hover:border-line-strong hover:text-fg"
              }`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function WhoSendsCard() {
  const hidden = useHidden();
  const [key, setKey] = useState(TOUR[0]);
  const [index, setIndex] = useState(0);
  const [tour, setTour] = useState(true);
  const [playing, setPlaying] = useState(true);
  // Mail jumps (no glide) when a scene starts over.
  const [jump, setJump] = useState(true);
  const reduced = useReducedMotion();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const scene = sceneFor(key);
  const last = scene.frames.length - 1;
  const frame = scene.frames[reduced ? last : Math.min(index, last)];

  const start = useCallback((next: string, asTour: boolean) => {
    setKey(next);
    setIndex(0);
    setTour(asTour);
    setJump(true);
    setPlaying(true);
  }, []);

  useEffect(() => {
    if (!jump) return;
    const id = requestAnimationFrame(() => setJump(false));
    return () => cancelAnimationFrame(id);
  }, [jump]);

  useEffect(() => {
    if (hidden || reduced || !playing) return;
    const wait = index === last ? HOLD_MS : STEP_MS;
    timer.current = setTimeout(() => {
      if (index < last) setIndex(index + 1);
      else start(tour ? nextInTour(key) : key, tour);
    }, wait);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [hidden, reduced, playing, index, last, key, tour, start]);

  const toggleHidden = (next: boolean) => writeHidden(next);

  if (hidden) {
    return (
      <button
        type="button"
        onClick={() => toggleHidden(false)}
        className="mt-6 text-sm font-medium text-accent underline-offset-2 hover:underline"
      >
        Show who writes and who sends
      </button>
    );
  }

  const tone = TONE[scene.tab];
  const chip = frame.chip;
  const [cx, cy] = chip ? CHIP_AT[chip.at] : CHIP_AT.inbox;

  return (
    <section aria-labelledby="who-sends-title" className="mt-6 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="who-sends-title" className="text-base font-semibold text-fg">
            Who writes and who sends
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-fg-muted">
            Act as me writes as you, and you send. Handle it for me sends as you on its own. Take the lead sends in
            the Executive&apos;s name.
          </p>
        </div>
        <button
          type="button"
          onClick={() => toggleHidden(true)}
          className="whitespace-nowrap text-sm font-medium text-fg-muted hover:text-fg"
        >
          Hide
        </button>
      </div>

      <div className="@container mt-4">
        <div
          role="img"
          aria-label={`${TAB_NAMES[scene.tab]}, ${scene.option}: ${scene.line}`}
          className="relative aspect-[4/3] w-full overflow-hidden rounded-xl bg-surface-base text-[3.1cqw] @min-[520px]:aspect-[5/2] @min-[520px]:text-[1.9cqw]"
        >
          <svg aria-hidden viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 h-full w-full">
            <g className="stroke-line-strong" strokeWidth="1.5" strokeDasharray="5 5">
              <line x1="15" y1="30" x2="85" y2="30" vectorEffect="non-scaling-stroke" />
              <line x1="50" y1="30" x2="50" y2="76" vectorEffect="non-scaling-stroke" />
              <line x1="50" y1="76" x2="85" y2="30" vectorEffect="non-scaling-stroke" />
            </g>
          </svg>
          <Node spot="inbox" label="Inbox" frame={{ ...frame, caps: { ...frame.caps, inbox: "Your mailbox" } }} tone={scene.tab} />
          <Node spot="exec" label="Executive" frame={frame} tone={scene.tab} round />
          <Node spot="you" label="You" frame={frame} tone={scene.tab} />
          <Node spot="to" label={frame.to ?? "Dana"} frame={frame} tone={scene.tab} />

          <div
            aria-hidden
            className={`absolute z-10 w-max max-w-[16em] -translate-x-1/2 -translate-y-1/2 rounded-[.6em] px-[.7em] py-[.45em] text-left text-[.9em] font-semibold leading-tight text-neutral-900 shadow-lg ${
              chip?.tint ? tone.chip : "bg-white dark:bg-neutral-100"
            } ${chip ? "opacity-100" : "opacity-0"} ${
              jump ? "" : "transition-[left,top,opacity] duration-1000 ease-[cubic-bezier(.6,0,.3,1)]"
            }`}
            style={{ left: `${cx}%`, top: `${cy}%` }}
          >
            <span className="block text-[.82em] font-medium text-neutral-600">{chip?.from}</span>
            {chip?.text}
          </div>

          {[
            ["exec", frame.execSees, 56],
            ["you", frame.youSays, 93],
          ].map(([id, text, top]) => (
            <div
              key={id as string}
              aria-hidden
              className={`absolute z-20 -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-full border px-[.7em] py-[.35em] text-[.85em] font-semibold transition-opacity duration-500 ${tone.text} ${tone.pill} ${
                text ? "opacity-100" : "opacity-0"
              }`}
              style={{ left: "50%", top: `${top}%`, borderColor: "currentColor" }}
            >
              {text}
            </div>
          ))}

          <div
            aria-hidden
            className={`absolute bottom-[6%] right-[3%] w-[30%] rounded-[.7em] border border-line bg-surface-overlay px-[.7em] py-[.55em] text-[.82em] transition-opacity duration-500 ${
              frame.sent ? "opacity-100" : "opacity-0"
            }`}
          >
            <b className={`mb-[.2em] block font-display ${TONE.handle.text}`}>What it sent</b>
            <span className="block text-fg">{frame.sent}</span>
          </div>
        </div>
      </div>

      <div className="mt-4 min-h-[4.5em]" aria-live="polite">
        <span className={`inline-block rounded-full px-2.5 py-0.5 font-display text-[15px] font-bold ${tone.text} ${tone.pill}`}>
          {TAB_NAMES[scene.tab]} · {scene.option}
        </span>
        <p className="mt-1.5 text-[15px] leading-relaxed text-fg">{scene.line}</p>
      </div>

      <div className="mt-3 space-y-3">
        <Options title="What it writes as you (Act as me)" group="writes" options={WRITE_OPTIONS} current={scene.writes} tone="act" onPick={(k) => start(k, false)} />
        <Options title="What it sends on its own (Handle it for me)" group="sends" options={SEND_OPTIONS} current={scene.sends} tone="handle" onPick={(k) => start(k, false)} />
        <Options title="Take the lead (as the Executive)" group="lead" options={LEAD_OPTIONS} current={scene.lead} tone="lead" onPick={(k) => start(k, false)} />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {!reduced && (
          <button
            type="button"
            onClick={() => setPlaying(!playing)}
            className="rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface-hover"
          >
            {playing ? "Pause" : "Play"}
          </button>
        )}
        {!tour && (
          <button
            type="button"
            onClick={() => start(TOUR[0], true)}
            className="rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface-hover"
          >
            Play all
          </button>
        )}
        <span className="text-[13px] text-fg-muted">Tap an option to see what it does.</span>
      </div>
    </section>
  );
}
