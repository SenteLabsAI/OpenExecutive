import Link from "next/link";

import type { FirstWeekCard as FirstWeekCardData } from "@/lib/api";

import { buttonClass } from "../ui/Button";

// Home's card for the install's first week: a goal to set while none is, then
// what the research setup ran found, then where to see what it watches. All
// text but the findings is fixed, so the card never narrates the system.

export default function FirstWeekCard({ card }: { card: FirstWeekCardData }) {
  const { needs_goal: needsGoal, findings, brief_tomorrow: briefTomorrow } = card;
  return (
    <section
      aria-labelledby="sec-first-week"
      className="mb-6 rounded-2xl border border-accent/25 bg-accent/5 px-4 py-4 sm:px-5"
    >
      <h2 id="sec-first-week" className="text-xs font-semibold uppercase tracking-wider text-accent">
        Your first week
      </h2>
      {needsGoal && (
        <>
          <p className="mt-2 text-base sm:text-lg font-semibold leading-snug text-fg">
            Give me one goal with a date, and I&apos;ll track it every day.
          </p>
          <Link href="/goals?add=1" className={buttonClass("primary", "md", "mt-3")}>
            Set a goal
          </Link>
        </>
      )}
      {findings.length > 0 && (
        <>
          <p className={`${needsGoal ? "mt-4" : "mt-2"} text-[15px] text-fg-muted`}>
            While you set up, I researched your company and industry:
          </p>
          <ul className="mt-2 space-y-2">
            {findings.map((f) => (
              <li key={f.title} className="flex gap-2 text-[15px] leading-relaxed text-fg">
                <span aria-hidden className="mt-[0.6em] h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent" />
                <span>
                  <span className="font-semibold">{f.title}</span> — {f.summary}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[15px] leading-relaxed text-fg">
            I keep watching these companies and topics, and tell you when one moves.{" "}
            <Link href="/watchlist" className="font-medium text-accent hover:underline">
              See what I&apos;m watching →
            </Link>
          </p>
        </>
      )}
      {briefTomorrow && <p className="mt-3 text-sm text-fg-muted">Your first morning brief comes tomorrow.</p>}
    </section>
  );
}
