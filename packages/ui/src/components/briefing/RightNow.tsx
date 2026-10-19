"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Today } from "@/lib/api";
import { MEMORY_ACTIONS, briefingMemoryLine } from "@/lib/briefing-memory";
import { clockLabel, isNewSince, newSinceLabel, type TimedPoint } from "@/lib/rightNow";

import { buildNarrativeSeed, type ContinueHandler } from "./shared";

// Right now: the Executive's live read of the day (the /today header, which
// is rewritten as things change), shown as a card at the top of Home with
// the whole picture in a side panel. Points that appeared since the reader
// last looked today get a green dot.

// How many points the Home card shows; the panel has them all.
const CARD_POINTS = 3;

// One line of Markdown (a point's bold subject, a link) without the block
// wrapper, so it sits inside a row.
function Inline({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ p: ({ children }) => <>{children}</> }}>
      {text}
    </ReactMarkdown>
  );
}

// Flatten a point's Markdown to the plain words handed to chat.
function plainText(text: string): string {
  return text.replace(/\*\*|__|`/g, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").trim();
}

function PointRow({
  point,
  isNew,
  onContinue,
}: {
  point: TimedPoint;
  isNew: boolean;
  onContinue?: ContinueHandler;
}) {
  const when = clockLabel(point.since);
  const body = (
    <>
      <span
        aria-hidden
        className={`mt-[0.45rem] h-2 w-2 flex-shrink-0 rounded-full ${isNew ? "bg-emerald-500" : "bg-line-strong"}`}
      />
      <span className="min-w-0 flex-1 text-[15px] text-fg [&_strong]:font-semibold">
        <Inline text={point.text} />
        {isNew && <span className="sr-only"> (new)</span>}
      </span>
      {when && <span className="flex-shrink-0 pt-0.5 text-xs tabular-nums text-fg-muted">{when}</span>}
    </>
  );
  if (!onContinue) {
    return <li className="flex items-start gap-2.5 border-t border-line py-2 first:border-t-0">{body}</li>;
  }
  const text = plainText(point.text);
  return (
    <li className="border-t border-line first:border-t-0">
      <button
        type="button"
        onClick={() => onContinue(buildNarrativeSeed(text), briefingMemoryLine(MEMORY_ACTIONS.narrative, text))}
        aria-label={`Discuss: ${text}`}
        className="-mx-2 flex w-[calc(100%+1rem)] items-start gap-2.5 rounded-xl px-2 py-2 text-left transition hover:bg-accent/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
      >
        {body}
      </button>
    </li>
  );
}

function Lead({ today }: { today: Today }) {
  const lead = today.narrative_lead;
  if (lead) return <p className="text-[17px] font-semibold leading-snug text-fg">{lead}</p>;
  // An older API without the split: the whole header as written.
  return (
    <div className="prose prose-invert max-w-none text-[15px] prose-p:my-1 prose-strong:text-fg">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{today.narrative ?? ""}</ReactMarkdown>
    </div>
  );
}

/** The Home card: when it was updated, what's new, the opener and the top
 * points. Renders nothing until there is a header or one is being written. */
export function RightNowCard({
  today,
  baseline,
  updatedLabel,
  onOpen,
}: {
  today: Today;
  baseline: Date | null;
  updatedLabel: string | null;
  onOpen: () => void;
}) {
  const points = today.narrative_points ?? [];
  const badge = newSinceLabel(points, baseline);
  return (
    <section aria-labelledby="sec-right-now" className="mb-8 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="mb-2 flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <h2 id="sec-right-now" className="text-lg font-semibold text-fg">
          Right now
        </h2>
        {updatedLabel && (
          <span className="whitespace-nowrap text-sm text-fg-muted" aria-live="polite">
            {updatedLabel}
          </span>
        )}
        {badge && (
          <span className="whitespace-nowrap rounded-full bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-700 dark:text-emerald-300">
            {badge}
          </span>
        )}
      </div>
      {today.narrative ? (
        <>
          <Lead today={today} />
          {points.length > 0 && (
            <ul className="mt-2">
              {points.slice(0, CARD_POINTS).map((p, i) => (
                <PointRow key={`${i}-${p.text}`} point={p} isNew={isNewSince(p, baseline)} />
              ))}
            </ul>
          )}
          <button
            type="button"
            onClick={onOpen}
            aria-haspopup="dialog"
            className="mt-3 min-h-[40px] rounded-xl px-1 text-[15px] font-semibold text-accent hover:underline"
          >
            See the whole picture →
          </button>
        </>
      ) : (
        <p className="animate-pulse text-[15px] text-fg-muted">Catching up on today…</p>
      )}
    </section>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-1 mt-5 text-xs font-bold uppercase tracking-wide text-fg-muted">{children}</h3>;
}

function calendarWhen(start: string | null | undefined, allDay: boolean | undefined): string {
  if (allDay || !start) return "All day";
  return clockLabel(start) ?? "";
}

/** The side panel: the opener, the points split into new and still open,
 * the Move today line, what's next on the calendar and the morning brief. */
export function RightNowBody({
  today,
  baseline,
  onContinue,
}: {
  today: Today;
  baseline: Date | null;
  onContinue?: ContinueHandler;
}) {
  const [showMorning, setShowMorning] = useState(false);
  if (!today.narrative) {
    return (
      <div aria-live="polite" className="animate-pulse rounded-xl bg-accent/5 px-4 py-3 text-[15px] text-fg-muted">
        {today.narrative_stale ? "Catching up on today…" : "Nothing yet today."}
      </div>
    );
  }
  const points = today.narrative_points ?? [];
  const fresh = points.filter((p) => isNewSince(p, baseline));
  const open = points.filter((p) => !isNewSince(p, baseline));
  const calendar = today.calendar_next ?? [];
  const morning = today.morning_brief;
  const list = (items: TimedPoint[], isNew: boolean) => (
    <ul>
      {items.map((p, i) => (
        <PointRow key={`${i}-${p.text}`} point={p} isNew={isNew} onContinue={onContinue} />
      ))}
    </ul>
  );
  return (
    <div>
      <Lead today={today} />
      {fresh.length > 0 && baseline ? (
        <>
          <SectionLabel>New since {clockLabel(baseline.toISOString())}</SectionLabel>
          {list(fresh, true)}
          {open.length > 0 && (
            <>
              <SectionLabel>Still open</SectionLabel>
              {list(open, false)}
            </>
          )}
        </>
      ) : (
        open.length > 0 && <div className="mt-3">{list(open, false)}</div>
      )}
      {today.narrative_move && (
        <p className="mt-4 rounded-xl bg-accent/5 px-3 py-2.5 text-[15px] text-fg">
          <span className="font-semibold">Move today:</span> {today.narrative_move}
        </p>
      )}
      {calendar.length > 0 && (
        <>
          <SectionLabel>Next on your calendar</SectionLabel>
          <ul>
            {calendar.map((e, i) => (
              <li key={`${i}-${e.title}`} className="flex items-start gap-2.5 border-t border-line py-2 first:border-t-0">
                <span className="w-16 flex-shrink-0 text-[15px] font-semibold tabular-nums text-fg">
                  {calendarWhen(e.start, e.all_day)}
                </span>
                <span className="min-w-0 flex-1 text-[15px] text-fg">{e.title}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {onContinue && points.length > 0 && (
        <p className="mt-4 text-sm text-fg-muted">Tap a point to talk it through.</p>
      )}
      {morning && (
        <div className="mt-5 border-t border-line pt-3">
          <button
            type="button"
            onClick={() => setShowMorning((v) => !v)}
            aria-expanded={showMorning}
            className="min-h-[40px] text-sm font-medium text-fg-muted hover:text-fg"
          >
            Earlier today: {clockLabel(morning.at)} morning brief {showMorning ? "▾" : "›"}
          </button>
          {showMorning && (
            <div className="prose prose-invert mt-2 max-w-none text-[15px] prose-p:my-2 prose-ul:my-2 prose-headings:text-fg prose-strong:text-fg">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{morning.text}</ReactMarkdown>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
