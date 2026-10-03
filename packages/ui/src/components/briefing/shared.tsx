"use client";

import Link from "next/link";

import type { ProposalItem } from "@/lib/api";

// Pieces every part of the Home briefing shares: the chat hand-off type, the
// seeds it sends, the bulk-dismiss cutoffs and a few small renderers.

// Hands a briefing item to chat. `memoryText` is what peer memory records as
// the user's words for that turn; omit it when `prompt` is already just the
// user's own ask.
export type ContinueHandler = (prompt: string, memoryText?: string) => void;

// Bulk-dismiss cutoffs (days): Needs you's ⋯ menu and the Monitoring panel.
export const NEEDS_YOU_DISMISS_OLDER_THAN_DAYS = 7;
export const MONITORING_DISMISS_OLDER_THAN_DAYS = 3;

// Ids of proposals older than `days` — what a bulk "Dismiss older than"
// sends: explicit ids from the caller's own lane (visible cards and those
// behind "Show more" alike), never a server-side age sweep, which would also
// hit teammates' routed items.
export function olderThan(proposals: ProposalItem[], days: number, now: Date = new Date()): number[] {
  const cutoff = now.getTime() - days * 86400000;
  return proposals
    .filter((p) => {
      // Same age anchor as the server's TTL: a re-firing situation is not old.
      const t = Date.parse(p.last_seen_at ?? p.created_at);
      return !Number.isNaN(t) && t < cutoff;
    })
    .map((p) => p.alert_id);
}

// Future-relative label for a pending run time ("in 8h"). Past/blank →
// "soon" (the caller renders "overdue" separately via the backend flag).
export function formatFuture(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "soon";
  const mins = Math.round((then - Date.now()) / 60000);
  if (mins <= 0) return "soon";
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  return `in ${Math.round(hrs / 24)}d`;
}

export function formatRelTime(iso: string): string {
  try {
    const diff = new Date(iso).getTime() - Date.now();
    const abs = Math.abs(diff);
    if (abs < 60_000) return "now";
    if (abs < 3_600_000) return `${Math.round(abs / 60_000)}m`;
    if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)}h`;
    return `${Math.round(abs / 86_400_000)}d`;
  } catch {
    return "—";
  }
}

// Seed prompt for a clicked narrative bullet. The Executive receives the
// open-alert digest as a <briefing> block on every chat turn, so the seed only
// needs to name the item — the exec matches it by headline. No alert_id is
// carried (the bullet has none); acking stays on the proposal card.
export function buildNarrativeSeed(text: string): string {
  return (
    `Let's dig into this from today's briefing:\n\n"${text}"\n\n` +
    `[Discuss mode] Walk me through what's going on, why it matters, and what ` +
    `you'd recommend. The full details are in your briefing context. Answer ` +
    `conversationally — don't take any action unless I explicitly ask.`
  );
}

// Discuss-handoff seed for a passive monitoring signal — shared by the
// ProposalCard monitoring branch and the Monitoring panel so the two entry
// points stay in sync (don't approve, just interpret the signal).
export function buildMonitoringSeed(proposal: ProposalItem): string {
  const body = proposal.body || proposal.headline;
  return (
    `Help me understand this signal we're monitoring:\n\n${body}\n\n` +
    `[Discuss mode — alert_id=${proposal.alert_id}] This is a passive ` +
    `monitoring signal, not a proposal to approve. Explain why it matters, ` +
    `whether it warrants any action, and what you'd recommend. Answer ` +
    `conversationally. Only if I explicitly ask you to act should you do ` +
    `more than advise; do not ack or dismiss the alert yourself.`
  );
}

// Status colours that read on both themes: the light theme gets the darker
// shade, the dark theme the lighter one.
export const TONE = {
  amber: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  rose: "bg-rose-500/15 text-rose-700 dark:text-rose-300",
  emerald: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  sky: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  neutral: "bg-surface-overlay text-fg-muted",
} as const;

export const TONE_TEXT = {
  amber: "text-amber-700 dark:text-amber-300",
  rose: "text-rose-700 dark:text-rose-300",
  emerald: "text-emerald-700 dark:text-emerald-300",
  sky: "text-sky-700 dark:text-sky-300",
} as const;

// A small status chip: a word on a soft tint.
export function Chip({
  tone = "neutral",
  title,
  children,
}: {
  tone?: keyof typeof TONE;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-lg px-2 py-0.5 text-[13px] font-medium ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}

// Either a button (when onContinue is provided — seeds chat with prompt)
// or a Link (no chat to hand to). Same visual styling either way.
export function ClickableBriefingItem({
  onContinue,
  prompt,
  href,
  className,
  children,
}: {
  onContinue?: ContinueHandler;
  prompt: string;
  href: string;
  className: string;
  children: React.ReactNode;
}) {
  if (onContinue) {
    return (
      <button
        type="button"
        onClick={() => onContinue(prompt)}
        className={`${className} text-left w-full cursor-pointer`}
      >
        {children}
      </button>
    );
  }
  return (
    <Link href={href} className={className}>
      {children}
    </Link>
  );
}

// The muted explainer at the top of a side panel (what an InfoTip used to say
// next to the section heading).
export function PanelIntro({ children }: { children: React.ReactNode }) {
  return <p className="mb-4 text-sm leading-relaxed text-fg-muted">{children}</p>;
}

// A numbered row's number, shared by the solo focus lists.
export function RankBadge({ n }: { n: number }) {
  return (
    <span
      aria-hidden="true"
      className="mt-0.5 flex-shrink-0 w-6 h-6 rounded-full bg-accent/15 text-accent text-xs font-semibold tabular-nums flex items-center justify-center"
    >
      {n}
    </span>
  );
}
