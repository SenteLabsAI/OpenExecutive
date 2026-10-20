// The solo Briefing's "Top three today" and "This week's review" cards: their
// wording, kept apart from the component so `npm test` can check it (see
// scripts/rhythmCards.test.mjs). The data comes from GET /today/top-three
// and GET /today/weekly-review.
//
// Type-only imports, so the test can load this under
// `node --experimental-strip-types`.

import type { TopThreeItem, WeeklyReviewSummary } from "@/lib/api";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_MS = 24 * 60 * 60 * 1000;

const ISO_DATE = /\b(\d{4})-(\d{2})-(\d{2})\b/g;

/** The line under a top-three item: why it made the list, capitalised, with
 * its dates written like the Due soon card's ("overdue (was due 2026-09-23)"
 * → "Overdue (was due Sep 23)"; "at risk; target 3 months, now 2.1" → "At
 * risk; …"). */
export function topThreeWhy(item: Pick<TopThreeItem, "why">): string {
  const why = item.why.trim().replace(ISO_DATE, (whole, _y: string, m: string, d: string) => {
    const month = MONTHS[Number(m) - 1];
    return month ? `${month} ${Number(d)}` : whole;
  });
  return why ? why.charAt(0).toUpperCase() + why.slice(1) : "";
}

/** The time suggested for an item: the free block ("10:00–11:00"), "No free
 * time left today" when the calendar was read but the day is full, or ""
 * when no calendar was read (nothing to say). */
export function topThreeSlot(item: Pick<TopThreeItem, "slot">): string {
  if (item.slot == null) return "";
  return item.slot || "No free time left today";
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** When the review ran, in the viewer's calendar: "Ran today", "Ran
 * yesterday", "Ran Fri, Sep 18"; "" when the time can't be read. */
export function reviewRanLabel(iso: string, now: Date = new Date()): string {
  const ran = new Date(iso);
  if (Number.isNaN(ran.getTime())) return "";
  const days = Math.round((startOfDay(now) - startOfDay(ran)) / DAY_MS);
  if (days <= 0) return "Ran today";
  if (days === 1) return "Ran yesterday";
  return `Ran ${WEEKDAYS[ran.getDay()]}, ${MONTHS[ran.getMonth()]} ${ran.getDate()}`;
}

export interface ReviewExcerpt {
  /** "Next week's top three" over a numbered list, or null over plain lines. */
  heading: string | null;
  numbered: boolean;
  lines: string[];
}

/** What the card shows of the review: next week's top three when it has
 * one, else the review's own short excerpt. */
export function reviewExcerpt(
  review: Pick<WeeklyReviewSummary, "top_three" | "excerpt">,
): ReviewExcerpt {
  const top = review.top_three.map((t) => t.trim()).filter(Boolean);
  if (top.length > 0) return { heading: "Next week's top three", numbered: true, lines: top };
  const lines = review.excerpt
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  return { heading: null, numbered: false, lines };
}
