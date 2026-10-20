// A named feature (Take the lead, Act as me, ...) set as a brand label: the
// display face in a pill with the feature's own color and glyph, the same on
// every screen. Use it where the name is a title or a tag; a name inside a
// sentence stays plain text. Sizes in em, so it scales with the heading or
// line it sits in.

import type { ReactNode } from "react";

export type Feature = "take_the_lead" | "act_as_me" | "handle_it" | "history" | "agent_council";

const FEATURES: Record<Feature, { name: string; tone: string; glyph: ReactNode }> = {
  take_the_lead: {
    name: "Take the lead",
    tone: "bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300",
    glyph: <path d="M3 8h9M8.5 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />,
  },
  act_as_me: {
    name: "Act as me",
    tone: "bg-indigo-100 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300",
    glyph: (
      <path
        d="M2.5 11.5c1.5-4 3-6 4-5s-1.5 4.5 0 4.5 2-3 3.5-3 1 2.5 3.5 1.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    ),
  },
  handle_it: {
    name: "Handle it for me",
    tone: "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300",
    glyph: (
      <g fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="8" cy="8" r="6" />
        <path d="M5.3 8.2l1.9 1.9 3.6-3.8" />
      </g>
    ),
  },
  history: {
    name: "History",
    tone: "bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300",
    glyph: (
      <g fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9" />
        <path d="M2.5 2.5v2.5H5M8 5v3l2 1.5" />
      </g>
    ),
  },
  agent_council: {
    name: "Agent Council",
    tone: "bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-500/15 dark:text-fuchsia-300",
    glyph: (
      <g fill="currentColor">
        <circle cx="8" cy="3.2" r="1.8" />
        <circle cx="12.6" cy="6.6" r="1.8" />
        <circle cx="10.9" cy="12" r="1.8" />
        <circle cx="5.1" cy="12" r="1.8" />
        <circle cx="3.4" cy="6.6" r="1.8" />
      </g>
    ),
  },
};

export default function FeatureName({ feature, className = "" }: { feature: Feature; className?: string }) {
  const f = FEATURES[feature];
  return (
    <span
      className={`inline-flex items-center gap-[0.35em] whitespace-nowrap rounded-full py-[0.15em] pl-[0.5em] pr-[0.65em] align-middle font-display font-bold leading-tight tracking-tight ${f.tone} ${className}`}
    >
      <svg viewBox="0 0 16 16" className="h-[1em] w-[1em] flex-none" aria-hidden>
        {f.glyph}
      </svg>
      {f.name}
    </span>
  );
}
