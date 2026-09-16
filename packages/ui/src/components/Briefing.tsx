"use client";

import Link from "next/link";
import { Children, useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  ackAlert,
  approveDecision,
  bulkAckAlerts,
  getToday,
  rejectDecision,
  reopenAlert,
  reviewAlerts,
  type DepartmentBriefItem,
  type HandledItem,
  type InFlightItem,
  type PersonBriefItem,
  type ClientCockpitCard,
  type ProposalItem,
  type Today,
} from "@/lib/api";
import { clientCountsSummary, renewalBadge } from "@/lib/practice";
import {
  HANDLED_REOPENABLE,
  groupHandled,
  handledAlsoLine,
  handledHeadline,
  handledKey,
  handledProofHref,
  isCloseKind,
  type HandledRow,
} from "@/lib/handled";
import InfoTip from "./InfoTip";
import { SectionHeading } from "./memories/shared";

// Future-relative label for a pending run time ("in 8h"). Past/blank →
// "soon" (the caller renders "overdue" separately via the backend flag).
function formatFuture(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "soon";
  const mins = Math.round((then - Date.now()) / 60000);
  if (mins <= 0) return "soon";
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  return `in ${Math.round(hrs / 24)}d`;
}

// High-volume sections render their full list inside a fixed-height scroll
// (max-h-[32rem] — like the Pulse "Recent activity" card) instead of capping
// at a "Show N more" expander, so no section grows the page. Count badges
// still reflect the full totals.
// Narrative bullets kept on screen before the rest fold into "Show N more".
const NARRATIVE_HEAD_BULLETS = 2;
// A proposal body longer than this (chars) is clamped to a few lines at rest
// with a Show more/less toggle, so the "Needs you" queue stays scannable.
const LONG_BODY_CHARS = 180;

// "Needs you" shows this many cards after "Start here" before a Show-more
// toggle takes over — a queue, not a wall.
const NEEDS_YOU_VISIBLE = 8;
// Bulk-dismiss cutoffs (days) offered under each lane.
const NEEDS_YOU_DISMISS_OLDER_THAN_DAYS = 7;
const MONITORING_DISMISS_OLDER_THAN_DAYS = 3;

// Compact age label ("3h", "9d") for a card chip; "" for an unparseable stamp.
function ageLabel(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const mins = Math.max(0, Math.round((now.getTime() - t) / 60000));
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

// Whole days between now and an ISO stamp: 0 = due within the day, negative =
// past (floor, so an 11-hour-old deadline is -1 → "overdue", never "due today").
// null if unparseable.
function daysUntil(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((t - now.getTime()) / 86400000);
}

// Ids of proposals older than `days` — what the bulk "Dismiss older than"
// footer sends: explicit ids from the caller's own lane (visible cards and
// those behind "Show more" alike), never a server-side age sweep, which would
// also hit teammates' routed items.
function olderThan(proposals: ProposalItem[], days: number, now: Date = new Date()): number[] {
  const cutoff = now.getTime() - days * 86400000;
  return proposals
    .filter((p) => {
      // Same age anchor as the server's TTL: a re-firing situation is not old.
      const t = Date.parse(p.last_seen_at ?? p.created_at);
      return !Number.isNaN(t) && t < cutoff;
    })
    .map((p) => p.alert_id);
}

interface BriefingProps {
  // Called when the user clicks a briefing item to continue the thread
  // in chat. The parent (usually the root page) is expected to switch
  // its main view from briefing → chat and seed the input with `prompt`.
  // When omitted, items render as plain navigation links so the standalone
  // /today page still works.
  onContinue?: (prompt: string) => void;
  // Set to true when this Briefing is the root landing surface — adds a
  // "Here's where we are" header. The standalone /today page already
  // gets the global AppShell breadcrumb, so it omits this prop.
  showHeader?: boolean;
  // Personalises the header greeting when showHeader is true.
  firstName?: string;
}

function formatRelTime(iso: string): string {
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

interface ClickableProps {
  onContinue?: (prompt: string) => void;
  prompt: string;
  href: string;
  className: string;
  children: React.ReactNode;
}

// Either a button (when onContinue is provided — seeds chat with prompt)
// or a Link (standalone /today behavior). Same visual styling either way.
function ClickableBriefingItem({ onContinue, prompt, href, className, children }: ClickableProps) {
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

// Flatten a react-markdown AST node to its plain text. Bullets in the
// "What's going on" narrative are `**Headline** — text`; this reads the
// underlying text nodes (recursing through bold/em/links) so a clicked bullet
// can be handed to the Executive as one clean string.
function nodeToPlainText(node: unknown): string {
  if (!node || typeof node !== "object") return "";
  const n = node as { value?: string; children?: unknown[] };
  if (typeof n.value === "string") return n.value;
  if (Array.isArray(n.children)) return n.children.map(nodeToPlainText).join("");
  return "";
}

// Seed prompt for a clicked narrative bullet. The Executive receives the
// open-alert digest as a <briefing> block on every chat turn, so the seed only
// needs to name the item — the exec matches it by headline. No alert_id is
// carried (the bullet has none); acking stays on the proposal card.
function buildNarrativeSeed(text: string): string {
  return (
    `Let's dig into this from today's briefing:\n\n"${text}"\n\n` +
    `[Discuss mode] Walk me through what's going on, why it matters, and what ` +
    `you'd recommend. The full details are in your briefing context. Answer ` +
    `conversationally — don't take any action unless I explicitly ask.`
  );
}

// Discuss-handoff seed for a passive monitoring signal — shared by the
// ProposalCard monitoring branch and the rail's MonitoringPanel so the two
// entry points stay in sync (don't approve, just interpret the signal).
function buildMonitoringSeed(proposal: ProposalItem): string {
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

function DeptCard({ dept, onContinue, dimmed = false }: { dept: DepartmentBriefItem; onContinue?: (p: string) => void; dimmed?: boolean }) {
  const hasIssues = dept.at_risk_count > 0 || dept.off_track_count > 0;
  const prompt = `Tell me about ${dept.title} — what's the current status?`;
  // Problem goals beyond the inline cap fall to the department page.
  const attentionGoalOverflow =
    dept.at_risk_count + dept.off_track_count - (dept.attention_goals?.length ?? 0);
  return (
    <ClickableBriefingItem
      onContinue={onContinue}
      prompt={prompt}
      href={`/departments/${dept.slug}`}
      className={`block group py-3 hover:bg-surface-overlay/30 transition-colors${dimmed ? " opacity-60 hover:opacity-100" : ""}`}
    >
      <div className="flex items-start justify-between gap-2 mb-3">
        <div className="text-sm font-semibold text-fg group-hover:text-indigo-300 transition-colors" title={dept.title}>
          {dept.title}
        </div>
        <span className="flex-shrink-0 text-[10px] text-fg-muted">{dept.authority_level.replace("_", " ")}</span>
      </div>
      <div className="flex items-center gap-3 text-xs">
        <span className="text-fg-muted">{dept.goal_count} Goal{dept.goal_count !== 1 ? "s" : ""}</span>
        {dept.at_risk_count > 0 && (
          <span className="inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-amber-500/20 text-amber-300 border-amber-500/30">
            {dept.at_risk_count} at risk
          </span>
        )}
        {dept.off_track_count > 0 && (
          <span className="inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-rose-500/20 text-rose-300 border-rose-500/30">
            {dept.off_track_count} off track
          </span>
        )}
        {!hasIssues && dept.goal_count > 0 && (
          <span className="inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-emerald-500/20 text-emerald-300 border-emerald-500/30">
            on track
          </span>
        )}
        {dept.awaiting_count > 0 && (
          <span className="ml-auto inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-sky-500/20 text-sky-300 border-sky-500/30">
            {dept.awaiting_count} awaiting
          </span>
        )}
      </div>
      {/* The actual off-track / at-risk goals, inline — so the card is
          insightful at rest instead of a count you have to click into.
          Healthy / inactive departments carry no attention_goals. */}
      {dept.attention_goals && dept.attention_goals.length > 0 && (
        <div className="mt-3 space-y-1.5 border-t border-line pt-2.5">
          {dept.attention_goals.map((g, i) => (
            <div key={i} className="flex items-start gap-1.5 text-[11px] leading-snug">
              <span
                aria-hidden="true"
                className={`mt-1 h-1.5 w-1.5 flex-shrink-0 rounded-full ${g.status === "off_track" ? "bg-rose-400" : "bg-amber-400"}`}
              />
              <span className="min-w-0">
                <span className="text-fg">{g.key_result}</span>
                {(g.current || g.target) && (
                  <span className="text-fg-subtle"> — {g.current || "—"} vs {g.target || "—"}</span>
                )}
              </span>
            </div>
          ))}
          {attentionGoalOverflow > 0 && (
            <div className="text-[10px] text-fg-subtle pl-3">+{attentionGoalOverflow} more</div>
          )}
        </div>
      )}
    </ClickableBriefingItem>
  );
}

// Compact label for an authority-scope token (see people/models.py).
const AUTHORITY_LABELS: Record<string, string> = {
  spend_lt_2k: "spend<2k",
  spend_lt_10k: "spend<10k",
  spend_gt_10k: "spend>10k",
  hiring_signoff: "hiring",
  vendor_onboarding: "vendors",
  customer_credit: "credit",
  legal_sign: "legal",
  board_comms: "board",
  wildcard: "all",
};

function authorityLabel(token: string): string {
  return AUTHORITY_LABELS[token] ?? token;
}

// Status chip text + colour. `overdue` repaints the attention states red.
function personStatusChip(person: PersonBriefItem): { label: string; cls: string } | null {
  const red = "bg-rose-500/20 text-rose-300 border-rose-500/30";
  const amber = "bg-amber-500/20 text-amber-300 border-amber-500/30";
  const sky = "bg-sky-500/20 text-sky-300 border-sky-500/30";
  const slate = "bg-slate-500/20 text-slate-300 border-slate-500/30";
  switch (person.status) {
    case "needs_reply":
      return {
        label: person.awaiting_reply_count > 1 ? `${person.awaiting_reply_count} awaiting reply` : "Awaiting reply",
        cls: person.overdue ? red : amber,
      };
    case "awaiting":
      return {
        label: `${person.awaiting_count} to action`,
        cls: person.overdue ? red : sky,
      };
    case "on_leave":
      return { label: "On leave", cls: slate };
    default:
      return null; // "clear" — no chip, shown as subtle text instead
  }
}

function PersonRow({ person }: { person: PersonBriefItem }) {
  const chip = personStatusChip(person);
  const pills = person.authority_scope.slice(0, 2);
  const extraPills = person.authority_scope.length - pills.length;
  return (
    <Link
      href={`/people/${person.id}`}
      className="block group py-3 hover:bg-surface-overlay/30 transition-colors"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-6 h-6 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center flex-shrink-0">
            <span className="text-white text-[9px] font-bold">{person.full_name.charAt(0)}</span>
          </div>
          <div className="min-w-0">
            <div className="text-xs font-medium text-fg truncate">{person.full_name}</div>
            <div className="text-[10px] text-fg-muted truncate">{person.role}</div>
          </div>
        </div>
        <div className="flex-shrink-0 text-right">
          {chip ? (
            <span className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${chip.cls}`}>
              {chip.label}
            </span>
          ) : (
            <span className="text-[10px] text-fg-subtle">Clear</span>
          )}
          {chip && person.status === "awaiting" && person.soonest_sla_at && (
            <div className={`text-[10px] mt-0.5 ${person.overdue ? "text-rose-300" : "text-fg-muted"}`}>
              SLA {person.overdue ? "overdue" : `in ${formatRelTime(person.soonest_sla_at)}`}
            </div>
          )}
        </div>
      </div>

      {person.insight && (
        <p className="mt-1.5 text-[10px] leading-snug text-fg-muted line-clamp-2">{person.insight}</p>
      )}

      <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
        {person.status !== "on_leave" && (
          <span className="inline-flex items-center gap-1 text-[10px] text-fg-subtle">
            <span className={`w-1.5 h-1.5 rounded-full ${person.reachable_now ? "bg-emerald-400" : "bg-slate-500"}`} />
            {person.reachable_now
              ? "Available"
              : person.next_window_at
                ? `Back in ${formatRelTime(person.next_window_at)}`
                : "Away"}
          </span>
        )}
        {pills.map((tok) => (
          <span key={tok} className="inline-block px-1 py-px rounded bg-surface-overlay text-[9px] text-fg-subtle border border-line">
            {authorityLabel(tok)}
          </span>
        ))}
        {extraPills > 0 && (
          <span className="text-[9px] text-fg-subtle">+{extraPills}</span>
        )}
      </div>
    </Link>
  );
}

// One-line roster summary shown under the People header.
function peopleSummary(people: PersonBriefItem[]): { text: string; hasOverdue: boolean } {
  const needsReply = people.filter((p) => p.status === "needs_reply").length;
  const awaiting = people.filter((p) => p.status === "awaiting").length;
  const onLeave = people.filter((p) => p.status === "on_leave").length;
  const overdue = people.filter((p) => p.overdue).length;
  const parts: string[] = [];
  if (needsReply > 0) parts.push(`${needsReply} to reply`);
  if (awaiting > 0) parts.push(`${awaiting} to action`);
  if (overdue > 0) parts.push(`${overdue} overdue`);
  if (onLeave > 0) parts.push(`${onLeave} on leave`);
  return { text: parts.length > 0 ? parts.join(" · ") : "All clear", hasOverdue: overdue > 0 };
}

// Section header label. `primary` makes a section visually dominant (the
// things that need the user's action); `ambient` is the quieter uppercase
// style used for awareness sections (in flight, monitoring, departments,
// activity). Count renders inline and only when non-zero.
function SectionLabel({
  variant,
  count,
  children,
}: {
  variant: "primary" | "ambient";
  count?: number;
  children: React.ReactNode;
}) {
  if (variant === "primary") {
    return (
      <span className="text-sm font-semibold text-fg">
        {children}
        {count != null && count > 0 && (
          <span className="ml-1.5 font-normal text-fg-muted">({count})</span>
        )}
      </span>
    );
  }
  return (
    <span className="text-xs font-semibold uppercase tracking-wide text-fg-muted group-open:text-fg">
      {children}
      {count != null && count > 0 && (
        <span className="ml-1 font-normal normal-case tracking-normal">({count})</span>
      )}
    </span>
  );
}

type StatTone = "indigo" | "rose" | "amber" | "sky" | "slate";
// `targetIds` are the section element ids a pill jumps to, in priority order
// (the first one that's actually present wins). Each section now renders once
// (the responsive grid stacks instead of duplicating into a mobile block), so
// these are effectively single-element, but the list shape is kept for headroom.
interface StatPill { label: string; tone: StatTone; targetIds: string[] }

const STAT_TONES: Record<StatTone, string> = {
  indigo: "bg-indigo-500/15 text-indigo-300 border-indigo-500/30",
  rose: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  amber: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  sky: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  // Quietest tone — passive monitoring signals, not something demanding action.
  slate: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

// Section ids the status pills scroll to. Kept beside the pill builder so the
// ids and the `id={…}` attributes on the sections stay in sync.
const SECTION_IDS = {
  needsYou: "sec-needs-you",
  handled: "sec-handled",
  inFlight: "sec-in-flight",
  monitoring: "sec-monitoring",
  departments: "sec-departments",
  people: "sec-people",
  practice: "sec-practice",
} as const;

// Smooth-scroll to the first rendered (visible) section in `ids`, popping open
// a collapsed <details> so the pill "shows the detail". No-ops gracefully when
// none of the targets are on the page (e.g. a 1-person org has no People block).
function scrollToFirstVisible(ids: string[]): void {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el && el.offsetParent !== null) {
      el.querySelector("details")?.setAttribute("open", "");
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
  }
}

// Glanceable status pills shown under the header — a one-second read of
// what needs attention, ordered most-urgent first. Zero counts are
// dropped; an empty result renders an "All clear" pill at the call site.
function briefingStats(args: {
  needsYou: number;
  handledOvernight: number;
  peopleOverdue: number;
  peopleNeedReply: number;
  deptAtRisk: number;
  inFlight: number;
  monitoring: number;
}): StatPill[] {
  const pills: StatPill[] = [];
  const peopleTargets = [SECTION_IDS.people];
  if (args.needsYou > 0)
    pills.push({ label: `${args.needsYou} need${args.needsYou === 1 ? "s" : ""} you`, tone: "indigo", targetIds: [SECTION_IDS.needsYou] });
  // What the Executive already did on its own — shown right after the ask,
  // so the first read is "N need you, it handled M" (trust + relief).
  if (args.handledOvernight > 0)
    pills.push({ label: `Executive handled ${args.handledOvernight} overnight`, tone: "sky", targetIds: [SECTION_IDS.handled] });
  if (args.peopleOverdue > 0)
    pills.push({ label: `${args.peopleOverdue} overdue`, tone: "rose", targetIds: peopleTargets });
  if (args.peopleNeedReply > 0)
    pills.push({ label: `${args.peopleNeedReply} awaiting reply`, tone: "amber", targetIds: peopleTargets });
  if (args.deptAtRisk > 0)
    pills.push({ label: `${args.deptAtRisk} dept${args.deptAtRisk === 1 ? "" : "s"} at risk`, tone: "amber", targetIds: [SECTION_IDS.departments] });
  if (args.inFlight > 0)
    pills.push({ label: `${args.inFlight} in flight`, tone: "sky", targetIds: [SECTION_IDS.inFlight] });
  // Passive watchlist signals — quietest pill, last, so it never crowds the
  // action-oriented ones but the lane is still reachable in one click.
  if (args.monitoring > 0)
    pills.push({ label: `${args.monitoring} monitoring`, tone: "slate", targetIds: [SECTION_IDS.monitoring] });
  return pills;
}

// Lifecycle presentation for one proposal card — the review-driven pieces the
// card composes: the one-line "what changed since you last looked", the chip
// row (age, seen ×N, why-now / due, likely-stale, folded-in, draft-ready), the
// muted "Reviewed <ago>" footer, and the single recommended move as a control.
// A pure builder (no hooks) so ProposalCard stays a layout function.
function buildProposalLifecycle({
  proposal,
  assignee,
  onContinue,
  busy,
}: {
  proposal: ProposalItem;
  assignee: PersonBriefItem | null | undefined;
  onContinue?: (p: string) => void;
  busy: boolean;
}): {
  reviewLine: React.ReactNode;
  lifecycleRow: React.ReactNode;
  reviewedFooter: React.ReactNode;
  moveButton: React.ReactNode;
} {
// Lifecycle signals from the Executive's review (alerts/review.py) and the
// coalescing pipeline: the one-line "what changed since you last looked",
// then chips — age, seen ×N, why-now / deadline, likely-stale, folded-in,
// updated — and a muted "Reviewed <ago>" footer.
const verdict = proposal.review_verdict ?? "";
const isLikelyStale = verdict === "likely_stale";
const age = ageLabel(proposal.created_at);
const dueIn = daysUntil(proposal.due_at);
const reviewLine = proposal.review_note ? (
  <p className={`mb-1.5 text-xs leading-snug ${isLikelyStale ? "text-amber-300" : "text-fg-muted"}`}>
    <span className="text-[10px] uppercase tracking-wide mr-1">
      {isLikelyStale ? "Likely stale" : verdict === "changed" ? "Updated" : "Since you last looked"}
    </span>
    {proposal.review_note}
  </p>
) : null;
const lifecycleChips: { label: string; cls: string; title?: string }[] = [];
if (age) lifecycleChips.push({ label: age, cls: "text-fg-subtle border-line", title: `Raised ${new Date(proposal.created_at).toLocaleString()}` });
if ((proposal.occurrence_count ?? 1) > 1)
  lifecycleChips.push({
    label: `seen ×${proposal.occurrence_count}`,
    cls: "text-fg-muted border-line",
    title: proposal.last_seen_at ? `Last seen ${ageLabel(proposal.last_seen_at)} ago` : undefined,
  });
if (proposal.why_now || dueIn != null) {
  const due = dueIn == null ? "" : dueIn < 0 ? "overdue" : dueIn === 0 ? "due today" : `due in ${dueIn}d`;
  lifecycleChips.push({
    label: [proposal.why_now, due].filter(Boolean).join(" · "),
    cls: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  });
}
if (isLikelyStale && !proposal.review_note)
  lifecycleChips.push({ label: "Likely stale", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" });
if ((proposal.superseded_count ?? 0) > 0)
  lifecycleChips.push({ label: `${proposal.superseded_count} folded in`, cls: "text-fg-muted border-line" });
if (verdict === "changed" && !proposal.review_note)
  lifecycleChips.push({ label: "Updated by the Executive", cls: "text-sky-300 border-sky-500/30" });
if (verdict === "drafted")
  lifecycleChips.push({ label: "Draft ready in your queue", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" });
const lifecycleRow = lifecycleChips.length > 0 ? (
  <div className="mb-1.5 flex flex-wrap gap-1">
    {lifecycleChips.map((c) => (
      <span key={c.label} title={c.title} className={`inline-block px-1.5 py-0.5 rounded border text-[10px] ${c.cls}`}>
        {c.label}
      </span>
    ))}
  </div>
) : null;
const reviewedFooter = proposal.last_reviewed_at ? (
  <p className="mt-1 text-[10px] text-fg-subtle">
    Reviewed {ageLabel(proposal.last_reviewed_at)} ago
    {verdict && verdict !== "likely_stale" ? ` · ${verdict}` : ""}
  </p>
) : null;
// The single recommended move, as the primary control on the card. Route /
// escalate / draft already happened server-side (the card shows their
// trace); nudge and a suggested workflow are the two the user completes.
const moveButton = (() => {
  const move = proposal.recommended_move ?? "";
  if (move === "nudge" && onContinue) {
    const who = assignee?.full_name ?? "the owner";
    return (
      <button
        type="button"
        onClick={() => onContinue(
          `Nudge ${who} about this item — it has gone quiet: ${proposal.headline}\n\n` +
          `Send a short, friendly check-in via message_person and tell me what you sent.`,
        )}
        disabled={busy}
        className="text-xs font-medium text-indigo-300 hover:text-indigo-200 px-2 py-1 rounded border border-indigo-500/30 transition-colors disabled:opacity-50"
      >
        ↪ Nudge {assignee?.full_name?.split(" ")[0] ?? "owner"}
      </button>
    );
  }
  if (move === "suggest_workflow" && proposal.suggested_workflow) {
    return (
      <Link
        href={`/jobs/${proposal.suggested_workflow}`}
        onClick={(e) => e.stopPropagation()}
        className="text-xs font-medium text-indigo-300 hover:text-indigo-200 px-2 py-1 rounded border border-indigo-500/30 transition-colors"
      >
        ▶ Run {proposal.suggested_workflow.replace(/_/g, " ")}
      </Link>
    );
  }
  return null;
})();
  return { reviewLine, lifecycleRow, reviewedFooter, moveButton };
}

function ProposalCard({
  proposal,
  people,
  onContinue,
  onApprove,
  onDismiss,
  onApproveWithEdits,
  busy = false,
  defaultBodyExpanded = false,
  emphasized = false,
}: {
  proposal: ProposalItem;
  people: PersonBriefItem[];
  onContinue?: (p: string) => void;
  onApprove?: (p: ProposalItem) => void;
  onDismiss?: (p: ProposalItem) => void;
  onApproveWithEdits?: (p: ProposalItem, editedBody: string) => void;
  busy?: boolean;
  // Start the long-body expander open (no clamp) — used by the elevated
  // "Start here" card so its body shows in full without a click.
  defaultBodyExpanded?: boolean;
  // Adds a left indigo accent to the flattened row — used for the
  // "Start here" proposal so the single sharpest item stands out without
  // breaking the divider-row rhythm.
  emphasized?: boolean;
}) {
  // Local edit-mode state. Entering edit mode replaces the body display
  // with a textarea pre-filled with the proposal body; the action row
  // simplifies to Cancel / Send approval. Exiting (Cancel) restores the
  // normal layout without acking anything. Send approval pipes the
  // edited text up through onApproveWithEdits so Briefing can ack the
  // alert + seed the chat with a verbatim-send instruction.
  const [editing, setEditing] = useState(false);
  const [editedBody, setEditedBody] = useState("");
  // Body expand/collapse for long action bodies (see isLongBody below). The
  // elevated "Start here" card opts out of clamping via defaultBodyExpanded.
  const [bodyOpen, setBodyOpen] = useState(defaultBodyExpanded);
  function startEditing() {
    setEditedBody(proposal.body || proposal.headline);
    setEditing(true);
  }
  function cancelEditing() {
    setEditing(false);
    setEditedBody("");
  }
  function submitEdit() {
    if (!onApproveWithEdits) return;
    const text = editedBody.trim();
    if (!text) return;
    onApproveWithEdits(proposal, text);
  }
  const assignee = proposal.routed_to_person_id != null
    ? people.find((p) => p.id === proposal.routed_to_person_id)
    : null;
  // Research artifacts (the Executive's `draft_artifact` tool) are full
  // authored documents, not terse proposals. The `artifact` topic tag is
  // the discriminator (mirrors the `external:*` tag convention). They get
  // a document layout: title heading + Markdown body + a "why this is
  // worth your time" block, and a review-not-approve action set.
  const isArtifact = proposal.topic_tags?.includes("artifact") ?? false;
  // Show the full body whenever the backend supplies it; fall back to
  // headline for older payloads that didn't carry body. Visible card
  // text wraps naturally and we no longer truncate mid-word.
  const displayText = proposal.body || proposal.headline;
  // Monitoring items are passive external/watchlist signals — there's nothing
  // to approve, so the card reframes the body as a "why it's on your radar"
  // rationale and drops the suggested-action ("If you approve:") block.
  const isMonitoring = proposal.category === "monitoring";
  // Decision-backed proposals (gated calendar bookings) execute server-side via
  // the /decisions endpoints — Approve books the meeting, Dismiss rejects it.
  // The verbatim-DM "Edit & approve" flow doesn't map to a calendar booking, so
  // it's hidden for these cards.
  const isDecision = proposal.decision_instance_id != null;
  // Long free-text action bodies are the briefing's other "wall of text".
  // Clamp them to a few lines at rest, with a Show more/less toggle, so the
  // "Needs you" queue stays scannable. Monitoring (bounded rationale) and
  // artifacts (their own scroll region) keep their existing treatment.
  const isLongBody = !isMonitoring && !isArtifact && displayText.length > LONG_BODY_CHARS;
  // Chat handoff prompt — seed the Executive with the FULL body PLUS
  // the suggested_action so it has the entire card's worth of context
  // (the "Reply to X: confirm Y..." instructions live in suggested_action
  // and previously got dropped on Discuss → the model would have to ask
  // the user to re-supply them).
  //
  // We also include a Discuss-mode primer telling the exec: stay
  // conversational until the user explicitly approves, then execute +
  // call ack_alert with the alert_id below. alert_id is needed so the
  // exec can clear the card from the briefing once approval lands.
  const handoffPrompt = (() => {
    if (isArtifact) {
      // Artifacts aren't approved/executed — they're read. Seed the chat
      // with the full document + rationale so the Executive can discuss
      // it, and let it clear the card via ack_alert when the user is done.
      const rationale = proposal.suggested_action
        ? `\n\nWhy you flagged it: ${proposal.suggested_action}`
        : "";
      return (
        `Let's discuss this artifact you flagged for my review:\n\n` +
        `# ${proposal.headline}\n\n${proposal.body || ""}${rationale}\n\n` +
        `[Discuss mode — alert_id=${proposal.alert_id}] This is a document ` +
        `for review, not an action to approve. Answer my questions about it ` +
        `conversationally. When I say I'm done ("got it", "reviewed", "thanks"), ` +
        `call ack_alert(alert_id=${proposal.alert_id}, status="ack") to clear ` +
        `it from my queue. Take no other action.`
      );
    }
    if (isMonitoring) {
      // Monitoring signals are passive — there's nothing to approve, so the
      // Discuss handoff asks the Executive to interpret the signal rather than
      // framing it as an approvable proposal. Dismiss is still available via
      // the card button (which acks "dismissed").
      return buildMonitoringSeed(proposal);
    }
    if (isDecision) {
      // Gated calendar booking. Approval/rejection happens via the card's
      // Approve/Dismiss buttons (which call the /decisions endpoints and book
      // or cancel the event server-side) — NOT via chat. So the Discuss
      // handoff is read-only: help the user decide, but take no action and do
      // not ack/approve from chat.
      const body = proposal.body || proposal.headline;
      return (
        `Let's talk through this meeting I've proposed:\n\n${body}\n\n` +
        `[Discuss mode] This booking is awaiting your approval on the ` +
        `briefing. Help me decide whether the time, attendees, and purpose ` +
        `make sense. Do NOT book, cancel, or ack anything from chat — I'll ` +
        `approve or dismiss it from the card itself.`
      );
    }
    const text = proposal.body || proposal.headline;
    const suggested = proposal.suggested_action
      ? `\n\nIf I approve, you will:\n${proposal.suggested_action}`
      : "";
    const primer = (
      `\n\n[Discuss mode — alert_id=${proposal.alert_id}] ` +
      `This proposal is NOT YET approved. Answer my questions conversationally. ` +
      `When I explicitly approve ("ok", "approve", "go ahead", "do it"), switch to ` +
      `execute mode: actually attempt the work (use web_search and your other tools — ` +
      `don't just promise), reply inline with the deliverable, schedule a fresh ` +
      `follow-up via schedule_followup if it's time-bound, and call ` +
      `ack_alert(alert_id=${proposal.alert_id}, status="ack"). ` +
      `If I dismiss it ("never mind", "drop it"), call ` +
      `ack_alert(alert_id=${proposal.alert_id}, status="dismissed") and stop. ` +
      `Until explicit approval/dismissal, take no action and do not ack.`
    );
    return `Tell me about this proposal:\n\n${text}${suggested}${primer}`;
  })();
  // Suggested-action block: visually promoted so the user reads it as a
  // commitment ("if I approve, the exec will do THIS") rather than a
  // footnote. Renders only when suggested_action is non-empty — and never
  // for monitoring items, where there's nothing to approve (the body is
  // reframed as a rationale in the content area instead).
  const suggestedActionBlock = (!isMonitoring && proposal.suggested_action) ? (
    <div className="mb-2 rounded-lg border border-indigo-500/30 bg-indigo-500/5 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-indigo-300 mb-1">
        {isArtifact ? "Why this is worth your time:" : "If you approve:"}
      </div>
      <p className="text-xs text-fg leading-snug whitespace-pre-wrap break-words">
        {proposal.suggested_action}
      </p>
    </div>
  ) : null;
  // Note explaining why an external/watchlist signal (large stock move) was
  // pulled into "Needs you" instead of Monitoring — set by the backend
  // (briefing/ranking.py) only for the severity-promoted case, so it reads
  // as deliberate rather than a routing bug.
  const surfacedNote = proposal.surfaced_reason ? (
    <p className="text-[10px] text-fg-muted leading-snug">
      {proposal.surfaced_reason}
    </p>
  ) : null;
  // The `artifact` tag is an internal UI discriminator (it drives the
  // document layout + amber badge), not a user-meaningful topic — hide it
  // from the tag pills so it doesn't double up with the "Artifact" badge.
  const visibleTags = proposal.topic_tags.filter((t) => t !== "artifact");
  const { reviewLine, lifecycleRow, reviewedFooter, moveButton } = buildProposalLifecycle({
    proposal, assignee, onContinue, busy,
  });
  const isLikelyStale = (proposal.review_verdict ?? "") === "likely_stale";
  const meta = (
    <>
      {reviewLine}
      {lifecycleRow}
      {suggestedActionBlock}
      {surfacedNote}
      {visibleTags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {visibleTags.map((t) => {
            // `external:*` tags come from the external-monitoring layer
            // — render in sky so the principal spots externally-sourced
            // proposals at a glance. Sky deliberately differs from the
            // amber elsewhere in the briefing (department at-risk,
            // awaiting-person chips) — those mean "action stalled on a
            // human"; this means "outside-world provenance".
            const isExternal = t.startsWith("external:");
            const cls = isExternal
              ? "inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-sky-500/20 text-sky-300 border-sky-500/30"
              : "inline-block px-1.5 py-0.5 rounded border text-[10px] text-fg-muted border-line";
            return (
              <span key={t} className={cls}>
                {t}
              </span>
            );
          })}
        </div>
      )}
      {reviewedFooter}
    </>
  );
  // Card splits into a content area and an action row living as siblings
  // inside a wrapper div so the explicit buttons don't nest inside the
  // outer click target (HTML disallows interactive-inside-interactive).
  //
  // Three render variants:
  // 1. `editing === true`: body becomes a textarea; action row is
  //    Cancel / Send approval only.
  // 2. `editing === false` + `onContinue`: body is a click target that
  //    seeds chat (Discuss); action row carries the explicit buttons.
  // 3. `editing === false` + no onContinue (standalone /today): body is
  //    static text; the explicit Discuss button is hidden.
  const showActions = Boolean(onApprove || onDismiss || onApproveWithEdits);
  // Left indigo accent for the "Start here" row (replaces the old ring on the
  // wrapper); applied to the flat row container in every render variant.
  const rowAccent = emphasized ? " border-l-2 border-indigo-500 pl-3" : "";
  if (editing) {
    return (
      <div className={`group py-3 hover:bg-surface-overlay/30 transition-colors${rowAccent}`}>
        <div className="flex items-start justify-between gap-2 mb-2">
          <span className="text-[10px] uppercase tracking-wide text-fg-subtle">Editing proposal — send verbatim</span>
          {assignee && (
            <span className="flex-shrink-0 text-xs text-indigo-300">
              → {assignee.full_name}
            </span>
          )}
        </div>
        {/* Show what the exec will do on approval so the user sees what
            they're authorizing while editing the message body. */}
        {suggestedActionBlock}
        <textarea
          value={editedBody}
          onChange={(e) => setEditedBody(e.target.value)}
          rows={6}
          disabled={busy}
          className="w-full text-sm text-fg bg-surface border border-line rounded-lg p-3 font-normal leading-snug whitespace-pre-wrap focus:outline-none focus:ring-1 focus:ring-indigo-500/40 disabled:opacity-50"
          autoFocus
        />
        <div className="mt-2 flex justify-end gap-2">
          <button
            type="button"
            onClick={cancelEditing}
            disabled={busy}
            className="text-xs text-fg-muted hover:text-fg px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submitEdit}
            disabled={busy || !editedBody.trim()}
            className="text-xs font-medium text-emerald-300 hover:text-emerald-200 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            ✓ Send approval
          </button>
        </div>
      </div>
    );
  }
  const assigneeBadge = assignee && (
    onContinue ? (
      <span className="flex-shrink-0 text-xs text-indigo-300">
        → {assignee.full_name}
      </span>
    ) : (
      <Link
        href={`/people/${assignee.id}`}
        className="flex-shrink-0 text-xs text-indigo-300 hover:underline"
        onClick={(e) => e.stopPropagation()}
      >
        → {assignee.full_name}
      </Link>
    )
  );
  const content = isArtifact ? (
    <>
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex items-center gap-2 min-w-0">
          <span className="flex-shrink-0 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border bg-amber-500/15 text-amber-300 border-amber-500/30">
            Artifact
          </span>
          <div className="text-sm font-medium text-fg break-words">{proposal.headline}</div>
        </div>
        {assigneeBadge}
      </div>
      {meta}
      {/* Full authored document, rendered as Markdown in a bounded,
          scrollable region so a long brief can't blow out the card. */}
      <div className="mt-2 max-h-72 overflow-y-auto rounded-lg border border-line bg-surface/40 px-3 py-2 prose prose-invert prose-sm max-w-none text-xs text-fg-muted leading-relaxed [&_*]:break-words">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {proposal.body || proposal.headline}
        </ReactMarkdown>
      </div>
    </>
  ) : (
    <>
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
          {isMonitoring ? (
            <>
              {/* Terse description first (headline), then the rationale the
                  triage body carries — only when it adds something beyond the
                  headline, so we don't render the same text twice. */}
              <div className="text-sm font-medium text-fg whitespace-pre-wrap break-words">
                {proposal.headline}
              </div>
              {proposal.body && proposal.body !== proposal.headline && (
                <>
                  <div className="mt-2 text-[10px] uppercase tracking-wide text-sky-300 mb-1">
                    Why this is on your radar
                  </div>
                  <div className="text-xs text-fg-muted whitespace-pre-wrap break-words">
                    {proposal.body}
                  </div>
                </>
              )}
            </>
          ) : (
            <div className={`text-sm font-medium text-fg whitespace-pre-wrap break-words${isLongBody && !bodyOpen ? " line-clamp-3" : ""}`} title={isLongBody && !bodyOpen ? displayText : undefined}>{displayText}</div>
          )}
        </div>
        {assigneeBadge}
      </div>
      {meta}
    </>
  );
  const contentArea = onContinue ? (
    <button
      type="button"
      onClick={() => onContinue(handoffPrompt)}
      className="block w-full text-left cursor-pointer transition-colors"
    >
      {content}
    </button>
  ) : (
    <div>{content}</div>
  );
  return (
    <div id={`alert-${proposal.alert_id}`} className={`group py-3 hover:bg-surface-overlay/30 transition-colors${rowAccent}`}>
      {contentArea}
      {/* Body expander — a sibling of the content area (never nested inside the
          Discuss click target, which HTML disallows) so a clamped long body can
          still be read in full without leaving the briefing. */}
      {isLongBody && (
        <button
          type="button"
          onClick={() => setBodyOpen((v) => !v)}
          aria-expanded={bodyOpen}
          className="pb-2 -mt-1 text-[11px] text-fg-muted hover:text-fg transition-colors cursor-pointer focus:outline-none focus:ring-1 focus:ring-indigo-500/40 rounded"
        >
          {bodyOpen ? "Show less" : "Show more"}
        </button>
      )}
      {showActions && (
        <div className="mt-2 flex items-center justify-between gap-2">
          {/* Discuss on the left — same handoff the body tap fires, but
              explicit so the affordance is discoverable. Hidden on the
              standalone /today route (no onContinue). */}
          <div className="flex items-center gap-2">
            {moveButton}
            {onContinue && (
              <button
                type="button"
                onClick={() => onContinue(handoffPrompt)}
                disabled={busy}
                className="text-xs text-fg-muted hover:text-indigo-300 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                💬 Discuss
              </button>
            )}
          </div>
          {/* Right-side actions. Artifacts are documents you review, not
              proposals you approve/execute — so they get a single
              "Mark reviewed" control (clears the card, no follow-on work)
              instead of the Dismiss / Edit / Approve trio. */}
          <div className="flex items-center gap-2">
            {isArtifact ? (
              onDismiss && (
                <button
                  type="button"
                  onClick={() => onDismiss(proposal)}
                  disabled={busy}
                  className="text-xs font-medium text-emerald-300 hover:text-emerald-200 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  ✓ Mark reviewed
                </button>
              )
            ) : (
              <>
                {onDismiss && (
                  <button
                    type="button"
                    onClick={() => onDismiss(proposal)}
                    disabled={busy}
                    className={`text-xs px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                      isLikelyStale
                        ? "font-medium text-amber-300 hover:text-amber-200 border border-amber-500/30"
                        : "text-fg-muted hover:text-rose-300"
                    }`}
                  >
                    ✕ Dismiss
                  </button>
                )}
                {/* Monitoring items are passive signals with no proposed
                    action to execute — only Discuss + Dismiss apply, so the
                    approve controls are hidden for them. */}
                {!isMonitoring && !isDecision && onApproveWithEdits && (
                  <button
                    type="button"
                    onClick={startEditing}
                    disabled={busy}
                    className="text-xs text-fg-muted hover:text-emerald-300 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    ✎ Edit &amp; approve
                  </button>
                )}
                {!isMonitoring && onApprove && (
                  <button
                    type="button"
                    onClick={() => onApprove(proposal)}
                    disabled={busy}
                    className="text-xs font-medium text-emerald-300 hover:text-emerald-200 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    ✓ Approve
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// In flight — what the Executive is about to do (scheduled follow-ups &
// nudges). Ambient/awareness content in the right column, always visible (no
// approval needed). Takes its anchor `id` as a prop (set by the caller).
function InFlightPanel({ inFlight, id }: { inFlight: InFlightItem[]; id?: string }) {
  if (inFlight.length === 0) return null;
  return (
    <section id={id} className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="flex items-center gap-1.5 mb-1">
        <SectionHeading title="In flight" count={inFlight.length} icon="bolt" />
        <InfoTip align="left">
          What the Executive is about to do (scheduled follow-ups &amp;
          nudges). Nothing here needs your approval — it&apos;s a heads-up.
        </InfoTip>
      </div>
      <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
        {inFlight.map((f) => (
          <div
            key={`if-${f.action_id}`}
            className={`group py-3 pl-2 border-l-2 hover:bg-surface-overlay/30 transition-colors ${f.overdue ? "border-amber-500/40" : "border-transparent"}`}
          >
            <div className="text-xs text-fg break-words" title={f.intent}>{f.intent}</div>
            <div className="mt-0.5 text-[11px] text-fg-muted">
              {f.target ? <span>→ {f.target}</span> : null}
              {f.department ? <span> · {f.department}</span> : null}
              <span>
                {" · "}
                {f.overdue ? (
                  <span className="text-amber-400">overdue</span>
                ) : (
                  formatFuture(f.run_at)
                )}
              </span>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// Multi-client practice mode only: rollup cards for PARKED client slots so
// the operator sees the whole practice from the active client's brief. The
// backend sends [] for single-company installs (0-1 slots), so this renders
// nothing in the default experience.
function PracticeClientsPanel({
  clients,
  id,
}: {
  clients: ClientCockpitCard[];
  id?: string;
}) {
  if (clients.length === 0) return null;
  return (
    <section id={id} className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="flex items-center gap-1.5">
          <SectionHeading title="Across your clients" count={clients.length} icon="building" />
          <InfoTip align="left">
            Your parked client companies. Counts reflect each client&apos;s last
            save point; switch to a client on the Clients page to work in it.
          </InfoTip>
        </div>
        <Link
          href="/clients"
          className="flex-shrink-0 text-xs text-indigo-400 hover:text-indigo-300"
        >
          Manage clients
        </Link>
      </div>
      <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
        {clients.map((c) => (
          <div key={`practice-${c.slug}`} className="py-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs font-medium text-fg truncate">
                {c.display_name}
                {c.role ? <span className="text-fg-muted font-normal"> · {c.role}</span> : null}
              </div>
              {(() => {
                const badge = renewalBadge(c.days_to_renewal);
                return badge ? (
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full border flex-shrink-0 ${
                    badge.urgent
                      ? "border-red-500/40 bg-red-500/10 text-red-400"
                      : "border-amber-500/40 bg-amber-500/10 text-amber-400"
                  }`}>
                    {badge.label}
                  </span>
                ) : null;
              })()}
            </div>
            <div className="mt-0.5 text-[11px] text-fg-muted">
              {clientCountsSummary(c)}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// A single passive monitoring signal as a compact rail row: headline +
// optional body excerpt, click-to-discuss, and a small dismiss affordance.
// Full ProposalCard is too wide for the rail, so this is the slimmed-down
// presentation; the Discuss handoff reuses buildMonitoringSeed so it behaves
// exactly like the card did.
function MonitoringRow({
  proposal,
  onContinue,
  onDismiss,
}: {
  proposal: ProposalItem;
  onContinue?: (p: string) => void;
  onDismiss?: (p: ProposalItem) => void;
}) {
  const seed = buildMonitoringSeed(proposal);
  const inner = (
    <>
      <div className="text-xs font-medium text-fg group-hover:text-indigo-300 transition-colors line-clamp-2" title={proposal.headline}>
        {proposal.headline}
      </div>
      {proposal.body && proposal.body !== proposal.headline && (
        <div className="mt-0.5 text-[11px] leading-snug text-fg-muted line-clamp-2">
          {proposal.body}
        </div>
      )}
    </>
  );
  return (
    <div id={`alert-${proposal.alert_id}`} className="relative group py-3 hover:bg-surface-overlay/30 transition-colors">
      {onContinue ? (
        <button
          type="button"
          onClick={() => onContinue(seed)}
          className="block w-full text-left pr-8 cursor-pointer focus:outline-none focus:ring-1 focus:ring-indigo-500/40"
        >
          {inner}
        </button>
      ) : (
        <Link href="/watchlist" className="block pr-8">
          {inner}
        </Link>
      )}
      {onDismiss && (
        <button
          type="button"
          aria-label="Dismiss signal"
          onClick={() => onDismiss(proposal)}
          className="absolute top-1.5 right-1.5 flex h-6 w-6 items-center justify-center rounded text-fg-subtle hover:text-fg hover:bg-surface-overlay cursor-pointer transition-colors opacity-0 group-hover:opacity-100 focus-within:opacity-100 focus:opacity-100 focus:outline-none focus:ring-1 focus:ring-indigo-500/40"
        >
          <span aria-hidden="true" className="text-sm leading-none">×</span>
        </button>
      )}
    </div>
  );
}

// Monitoring — passive signals (watchlist tickers, vendor status, external
// news) the Executive is tracking. An ambient right-column card; the full
// list scrolls inside the card. Takes its anchor `id` as a prop.
function MonitoringPanel({
  proposals,
  onContinue,
  onDismiss,
  onBulkDismiss,
  id,
}: {
  proposals: ProposalItem[];
  onContinue?: (p: string) => void;
  onDismiss?: (p: ProposalItem) => void;
  onBulkDismiss?: (ids: number[]) => void;
  id?: string;
}) {
  const staleIds = olderThan(proposals, MONITORING_DISMISS_OLDER_THAN_DAYS);
  if (proposals.length === 0) return null;
  return (
    <section id={id} className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="flex items-center gap-1.5 mb-1">
        <SectionHeading title="Monitoring" count={proposals.length} icon="eye" />
        <InfoTip align="left">
          Passive signals (watchlist tickers, vendor status, external news)
          the Executive is tracking. Nothing here needs a decision — tap one
          to talk it through.
        </InfoTip>
      </div>
      <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
        {proposals.map((p) => (
          <MonitoringRow key={p.alert_id} proposal={p} onContinue={onContinue} onDismiss={onDismiss} />
        ))}
      </div>
      {onBulkDismiss && staleIds.length > 0 && (
        <button
          type="button"
          onClick={() => onBulkDismiss(staleIds)}
          className="mt-2 text-[11px] text-fg-muted hover:text-rose-300 transition-colors"
        >
          ✕ Dismiss {staleIds.length} older than {MONITORING_DISMISS_OLDER_THAN_DAYS} days
        </button>
      )}
    </section>
  );
}

// Handled rail — pure logic lives in @/lib/handled (tested by `npm test`);
// only rendering stays here.

// First-person sentence for one row. Falls back to the audit summary when the
// row predates the structured fields (no headline / target to compose from).
function handledSentence(h: HandledItem): React.ReactNode {
  const headline = h.headline ?? "";
  const target = h.target ?? "";
  const H = <span className="text-fg">{headline}</span>;
  const T = <span className="text-fg">{target}</span>;
  const why = h.detail ? <span className="text-fg-subtle"> — {h.detail}</span> : null;
  if (!headline) return h.summary;
  switch (h.kind) {
    case "closed":
      return h.outcome === "dismissed" ? <>Dismissed {H} as stale{why}</> : <>Resolved {H}{why}</>;
    case "routed":
      if (!target) return h.summary;
      return h.outcome === "proposed"
        ? <>Proposed {H} to {T} <span className="text-fg-subtle">(awaiting their approval)</span></>
        : <>Handed {H} to {T}</>;
    case "nudged":
      return target ? <>Chased {T} on {H}</> : h.summary;
    case "escalated":
      return target ? <>Raised {H} to {T}{why}</> : <>Raised {H}{why}</>;
    case "drafted":
      return target ? <>Drafted {T} from {H}</> : h.summary;
    case "merged":
      return target ? <>Folded {H} into {T}</> : h.summary;
    case "suggested_workflow":
      return target ? <>Suggested running {T} on {H}</> : h.summary;
    case "watching":
      return <>Started watching {H}{why}</>;
    case "stopped_watching":
      return <>Stopped watching {H}{why}</>;
    default:
      return h.summary;
  }
}

// Jump to the alert's card; when it is folded behind "Show more" (no element
// yet), land on the queue section instead of clicking dead.
function scrollToAlertCard(alertId: number) {
  const target = document.getElementById(`alert-${alertId}`) ?? document.getElementById(SECTION_IDS.needsYou);
  target?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function HandledTrailerLink({ href, label }: { href: string; label: string }) {
  return (
    <>
      <span aria-hidden="true">·</span>
      <Link href={href} className="hover:text-indigo-300 transition-colors">{label}</Link>
    </>
  );
}

function HandledRowView({
  row,
  reverted,
  onReopen,
}: {
  row: HandledRow;
  reverted: boolean;
  onReopen?: (rowKey: string, alertId: number) => void;
}) {
  const h = row.item;
  // Undo only while the close still stands and the server would accept a
  // reopen (resolved / dismissed / expired / merged; "" = status unknown).
  const canUndo =
    Boolean(onReopen) && h.alert_id != null && isCloseKind(h) && !reverted && HANDLED_REOPENABLE.has(h.status ?? "");
  // "open" means live in the queue right now — the only state with a card to
  // jump to (an acked or snoozed alert has none).
  const stillOpen = h.alert_id != null && h.status === "open" && !isCloseKind(h);
  const proofHref = handledProofHref(h);
  const alsoLine = handledAlsoLine(row);
  return (
    <div className="py-2 flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className={`text-xs leading-snug ${reverted ? "line-through text-fg-subtle" : "text-fg-muted"}`}>
          {handledSentence(h)}
        </p>
        <p className="mt-0.5 text-[10px] text-fg-subtle flex flex-wrap items-center gap-x-1.5">
          {alsoLine && <span>{alsoLine}</span>}
          {alsoLine && <span aria-hidden="true">·</span>}
          <span>{ageLabel(h.at)} ago</span>
          {proofHref && <HandledTrailerLink href={proofHref} label={h.evidence_ref || "evidence"} />}
          {h.kind === "drafted" && <HandledTrailerLink href="/artifacts" label="read the draft" />}
          {(h.kind === "watching" || h.kind === "stopped_watching") && (
            <HandledTrailerLink href="/watchlist" label="watchlist" />
          )}
          {reverted && (
            <>
              <span aria-hidden="true">·</span>
              <span className="text-sky-300">Reopened</span>
            </>
          )}
        </p>
      </div>
      {canUndo && (
        <button
          type="button"
          onClick={() => onReopen?.(handledKey(h), h.alert_id as number)}
          className="flex-shrink-0 text-[11px] text-fg-muted hover:text-indigo-300 transition-colors"
        >
          ↶ Undo
        </button>
      )}
      {stillOpen && (
        <button
          type="button"
          onClick={() => scrollToAlertCard(h.alert_id as number)}
          className="flex-shrink-0 text-[11px] text-fg-muted hover:text-indigo-300 transition-colors"
          title="Jump to it in your queue"
        >
          still open ↓
        </button>
      )}
    </div>
  );
}

const HANDLED_DETAILS_ID = "sec-handled-details";

function HandledOvernightPanel({
  items,
  onReopen,
  undone,
}: {
  items: HandledItem[];
  onReopen?: (rowKey: string, alertId: number) => void;
  undone: Set<string>;
}) {
  const [open, setOpen] = useState(false);
  // The rail is rebuilt from the audit log on every fetch, so a close the
  // principal already undid still has its row: `status === "open"` (server
  // truth after a reload) or the in-session set marks it reverted.
  const reverted = (h: HandledItem) => isCloseKind(h) && (h.status === "open" || undone.has(handledKey(h)));

  // Guard on the grouped rows, not the raw items: a merge folded into a
  // listed survivor leaves no row of its own.
  const rows = groupHandled(items);
  if (rows.length === 0) return null;

  return (
    <section id={SECTION_IDS.handled} className="rounded-xl border border-line bg-surface-elevated px-4 py-2.5">
      <div className="flex items-start gap-1.5">
        <p className="min-w-0 flex-1 text-sm text-fg">{handledHeadline(rows, reverted)}</p>
        <InfoTip align="left">
          Moves I completed on my own since your last delivered brief — routed,
          chased, escalated, drafted, folded, or closed with cited evidence.
          Rewrites of open alerts show on the card itself, not here. Undo puts a
          closed item back in your queue.
        </InfoTip>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={HANDLED_DETAILS_ID}
          className="flex-shrink-0 mt-0.5 text-[11px] text-fg-muted hover:text-fg transition-colors"
        >
          {open ? "hide" : "details"} <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        </button>
      </div>
      {/* Always in the DOM so aria-controls resolves while collapsed. */}
      <div id={HANDLED_DETAILS_ID} hidden={!open} className="mt-1.5 divide-y divide-line border-t border-line">
        {open &&
          rows.map((row) => (
            <HandledRowView key={handledKey(row.item)} row={row} reverted={reverted(row.item)} onReopen={onReopen} />
          ))}
      </div>
    </section>
  );
}

export default function Briefing({ onContinue, showHeader = false, firstName }: BriefingProps) {
  const [today, setToday] = useState<Today | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The healthy ("on track") and inactive departments collapse into a single
  // quiet toggle so the section has one disclosure pattern instead of three.
  const [showAllDeptsOpen, setShowAllDeptsOpen] = useState(false);
  // Alerts the user has acted on this session. We optimistically drop
  // them from the rendered "Needs you" / "Across the team" lists so
  // the click feels instant; the canonical state will be picked up by
  // the next /today fetch.
  const [actedAlertIds, setActedAlertIds] = useState<Set<number>>(new Set());
  // "Needs you" shows NEEDS_YOU_VISIBLE cards after "Start here"; the rest
  // sit behind one toggle so a long queue reads as a queue, not a wall.
  const [showAllNeedsYou, setShowAllNeedsYou] = useState(false);
  // "Re-check relevance" runs the Executive's review on demand.
  const [recheckBusy, setRecheckBusy] = useState(false);
  // Handled-rail rows undone this session (keyed per row, see handledKey).
  const [undoneRows, setUndoneRows] = useState<Set<string>>(new Set());

  // Re-pull /today after a server-side mutation (e.g. a decision approve/reject)
  // so derived data — the narrative header, per-person and department counts —
  // re-syncs. The optimistic actedAlertIds set already hides the card; this
  // refreshes everything computed from it. Best-effort: a failed refresh leaves
  // the stale-but-still-usable view rather than erroring the briefing.
  const refreshToday = useCallback(() => {
    getToday().then(setToday).catch(() => { /* keep current view on failure */ });
  }, []);

  const handleApprove = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      // Decision-backed cards (gated calendar bookings) execute server-side:
      // approveDecision books the meeting AND clears the companion alert, so
      // there's no chat handoff — the optimistic removal hides the card and
      // the next /today fetch confirms it's gone.
      if (proposal.decision_instance_id != null) {
        await approveDecision(proposal.decision_instance_id);
        refreshToday();  // re-sync narrative + counts (no chat nav to trigger it)
        return;
      }
      await ackAlert(proposal.alert_id, "ack");
      if (onContinue) {
        const text = proposal.body || proposal.headline;
        const action = proposal.suggested_action
          ? `\n\nAction to perform:\n${proposal.suggested_action}`
          : "";
        onContinue(
          `I've approved this proposal — the alert is already acked, so do not call ack_alert. ` +
            `Now actually do the work: attempt the action below yourself using your tools ` +
            `(web_search for any research, specialist consults for analysis). Reply inline ` +
            `with the deliverable — the brief, the findings, the draft, whatever the action ` +
            `produces. If the watch is time-bound (e.g. earnings tomorrow, news to recheck), ` +
            `call schedule_followup so I get a fresh check at the right time. Do NOT just ` +
            `summarize what you would do, file it for later, or assign it to someone — ` +
            `the assignment IS to you.\n\nProposal:\n${text}${action}`,
        );
      }
    } catch (e) {
      // Revert the optimistic removal on failure so the user can retry.
      setActedAlertIds(prev);
      // eslint-disable-next-line no-console
      console.error("Approve failed", e);
    }
  }, [actedAlertIds, onContinue, refreshToday]);

  // Dismiss is a record-and-forget decision: the card disappears
  // immediately, the backend marks the alert ``dismissed`` (so the
  // Executive sees it on the next /today / alerts read and stops
  // re-surfacing the same thread), and the user stays on the briefing.
  // Unlike Approve, there is no follow-on work for the LLM to carry
  // out, so we deliberately do NOT seed a chat turn — that would
  // navigate away from the briefing for no benefit. On failure we
  // revert the optimistic removal so the user can see the card came
  // back and retry.
  const handleDismiss = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      // Decision-backed cards reject server-side (which also clears the
      // companion alert); ordinary alerts just get acked "dismissed".
      if (proposal.decision_instance_id != null) {
        await rejectDecision(proposal.decision_instance_id);
        refreshToday();  // re-sync narrative + counts (no chat nav to trigger it)
        return;
      }
      await ackAlert(proposal.alert_id, "dismissed");
    } catch (e) {
      setActedAlertIds(prev);
      // eslint-disable-next-line no-console
      console.error("Dismiss failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Bulk dismiss: the footer under a lane sends explicit ids (the cards the
  // caller can see), never a server-side age sweep. Same optimistic-removal
  // + rollback pattern as the single-card handlers.
  const handleBulkDismiss = useCallback(async (ids: number[]) => {
    if (ids.length === 0) return;
    const prev = actedAlertIds;
    const next = new Set(prev);
    ids.forEach((id) => next.add(id));
    setActedAlertIds(next);
    try {
      await bulkAckAlerts({ status: "dismissed", alert_ids: ids });
      refreshToday();
    } catch (e) {
      setActedAlertIds(prev);
      // eslint-disable-next-line no-console
      console.error("Bulk dismiss failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Re-check relevance: ask the Executive to review every open alert now
  // (route / escalate / draft / merge / resolve within authority), then
  // re-pull /today so the verdicts, chips and "handled" rail refresh.
  const handleRecheck = useCallback(async () => {
    setRecheckBusy(true);
    try {
      await reviewAlerts();
      refreshToday();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("Alert review failed", e);
    } finally {
      setRecheckBusy(false);
    }
  }, [refreshToday]);

  // Undo an autonomous close from the handled rail (HandledOvernightPanel). The rail is
  // rebuilt from the audit log on every fetch (the "closed" row persists after
  // a reopen), so a 409 "already open" after a reload counts as done.
  const handleReopen = useCallback(async (rowKey: string, alertId: number) => {
    try {
      await reopenAlert(alertId);
      setUndoneRows((prev) => new Set(prev).add(rowKey));
      refreshToday();
    } catch (e) {
      if (e instanceof Error && /409|already/i.test(e.message)) {
        setUndoneRows((prev) => new Set(prev).add(rowKey));
        return;
      }
      // eslint-disable-next-line no-console
      console.error("Reopen failed", e);
    }
  }, [refreshToday]);

  // Approve-with-edits: user has tweaked the draft text and wants OE to
  // send exactly what they wrote (no LLM rephrasing). Same optimistic-
  // removal + ack pattern as plain approve; the chat seed instructs the
  // Executive to use the edited body verbatim.
  const handleApproveWithEdits = useCallback(async (proposal: ProposalItem, editedBody: string) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await ackAlert(proposal.alert_id, "ack");
      if (onContinue) {
        onContinue(
          `I'm approving this proposal with my edits — the alert is already acked, so do not ` +
            `call ack_alert. Use the text below VERBATIM when you deliver the message: do not ` +
            `rephrase, summarize, or restructure it. Then go execute (send the DM/email, ` +
            `schedule any follow-up via schedule_followup) and tell me what you did.\n\n${editedBody}`,
        );
      }
    } catch (e) {
      setActedAlertIds(prev);
      // eslint-disable-next-line no-console
      console.error("Approve-with-edits failed", e);
    }
  }, [actedAlertIds, onContinue]);

  useEffect(() => {
    let cancelled = false;
    getToday()
      .then((t) => { if (!cancelled) setToday(t); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const dateLabel = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });

  const activeDepts = today?.departments.filter(
    (d) => d.goal_count > 0 || d.awaiting_count > 0
  ) ?? [];
  const inactiveDepts = today?.departments.filter(
    (d) => d.goal_count === 0 && d.awaiting_count === 0
  ) ?? [];
  // Within active departments, only the ones with a problem (at risk /
  // off track / awaiting) earn a card at rest. Healthy departments fold
  // into a quiet "N on track" toggle so the briefing isn't a wall of
  // green "on track" cards.
  const attentionDepts = activeDepts.filter(
    (d) => d.at_risk_count > 0 || d.off_track_count > 0 || d.awaiting_count > 0
  );
  const onTrackDepts = activeDepts.filter(
    (d) => d.at_risk_count === 0 && d.off_track_count === 0 && d.awaiting_count === 0
  );
  // Healthy + inactive departments fold into one quiet toggle. Summary text
  // reflects both buckets so the single control is self-describing.
  const quietDeptCount = onTrackDepts.length + inactiveDepts.length;
  const quietDeptSummary = [
    onTrackDepts.length > 0 ? `${onTrackDepts.length} on track` : null,
    inactiveDepts.length > 0 ? `${inactiveDepts.length} inactive` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const isQuiet =
    today !== null &&
    today.proposals.length === 0 &&
    activeDepts.every((d) => d.at_risk_count === 0 && d.off_track_count === 0 && d.awaiting_count === 0) &&
    today.people.every((p) => p.awaiting_count === 0);

  const showPeopleSidebar = (today?.people.length ?? 0) > 1;

  // Bucket proposals once (previously an inline IIFE in the JSX) so the
  // status strip and the section lists work off the same split. "Needs
  // you" = action proposals routed to the caller, plus unrouted catch-all
  // items when the caller is the principal. When the caller can't be
  // resolved (caller_person_id null — e.g. a direct curl), fall back to
  // the legacy single bucket so the queue still renders. Optimistically-
  // acted alerts are dropped first so counts reflect the click.
  const callerId = today?.caller_person_id ?? null;
  const isPrincipalCaller =
    callerId == null
      ? false
      : today?.people.find((p) => p.id === callerId)?.is_principal ?? false;
  const liveProposals = (today?.proposals ?? []).filter(
    (p) => !actedAlertIds.has(p.alert_id),
  );
  const monitoringProposals = liveProposals.filter((p) => p.category === "monitoring");
  const actionProposals = liveProposals.filter((p) => p.category !== "monitoring");
  const mineProposals =
    callerId == null
      ? actionProposals
      : actionProposals.filter(
          (p) =>
            p.routed_to_person_id === callerId ||
            (p.routed_to_person_id == null && isPrincipalCaller),
        );
  const mineIds = new Set(mineProposals.map((p) => p.alert_id));
  const otherProposals =
    callerId == null ? [] : actionProposals.filter((p) => !mineIds.has(p.alert_id));
  // "Needs you" splits into the single highest-priority item (rendered as an
  // elevated "Start here" card so there's one obvious first action) and the
  // rest (the scrolling queue below). Backend sorts action proposals by score,
  // so mineProposals[0] is the sharpest.
  const startHereProposal = mineProposals[0] ?? null;
  const restProposals = mineProposals.slice(1);
  const staleNeedsYouIds = olderThan(mineProposals, NEEDS_YOU_DISMISS_OLDER_THAN_DAYS);
  const handledOvernight = today?.handled_overnight ?? [];

  // Status-strip inputs, all from data already computed above.
  const inFlightCount = today?.in_flight?.length ?? 0;
  const deptAtRiskCount = attentionDepts.filter(
    (d) => d.at_risk_count > 0 || d.off_track_count > 0,
  ).length;
  const peopleNeedReply = (today?.people ?? []).filter((p) => p.status === "needs_reply").length;
  const peopleOverdue = (today?.people ?? []).filter((p) => p.overdue).length;
  const statPills: StatPill[] = today
    ? briefingStats({
        needsYou: mineProposals.length,
        handledOvernight: groupHandled(handledOvernight).length,
        peopleOverdue,
        peopleNeedReply,
        deptAtRisk: deptAtRiskCount,
        inFlight: inFlightCount,
        monitoring: monitoringProposals.length,
      })
    : [];

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-6xl mx-auto px-6 py-6">
          <div className="flex items-baseline gap-3 mb-3">
            <h1 className="text-xl font-semibold text-fg">
              {showHeader
                ? (firstName ? `Here's where we are, ${firstName}.` : "Here's where we are.")
                : "Today"}
            </h1>
            <span className="text-sm text-fg-muted">{dateLabel}</span>
          </div>

          {loading && <p className="text-fg-muted text-sm">Loading…</p>}
          {error && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm mb-4">
              {error}
            </div>
          )}

          {today && (
            <>
              {/* Glanceable status strip — a one-second read of what needs
                  attention, built from the buckets computed above. */}
              <div className="flex flex-wrap items-center gap-2 mb-6">
                {statPills.length === 0 ? (
                  <span className="inline-flex items-center rounded-full border border-emerald-500/30 bg-emerald-500/15 px-2.5 py-1 text-xs font-medium text-emerald-300">
                    All clear
                  </span>
                ) : (
                  statPills.map((pill) => (
                    <button
                      key={pill.label}
                      type="button"
                      onClick={() => scrollToFirstVisible(pill.targetIds)}
                      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium cursor-pointer transition hover:brightness-110 focus:outline-none focus:ring-1 focus:ring-indigo-500/40 ${STAT_TONES[pill.tone]}`}
                    >
                      {pill.label}
                    </button>
                  ))
                )}
              </div>

              {today.narrative && (
                <section className="mb-6">
                  <div className="flex items-center gap-1.5 mb-3">
                    <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
                      What&apos;s going on
                    </h2>
                    <InfoTip align="left">
                      The Executive&apos;s read on the company right now —
                      synthesized from proposals, at-risk goals, and recent
                      activity. Regenerates as the picture changes.
                    </InfoTip>
                  </div>
                  <div className="rounded-xl border border-indigo-500/20 bg-indigo-500/5 px-5 py-4">
                    <div className="prose prose-invert prose-sm max-w-none prose-p:my-1 prose-ul:my-1 prose-headings:text-fg prose-strong:text-fg">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={
                          onContinue
                            ? {
                                // Drop the default disc + marker padding so our
                                // 💬 acts as the bullet. Tailwind utilities beat
                                // the `prose` plugin's zero-specificity :where(ul)
                                // rules, so list-none/pl-0 win here.
                                //
                                // Hybrid declutter: keep the top 2 signals on
                                // screen and fold the rest into a quiet "Show N
                                // more" disclosure so the narrative stops being a
                                // wall of text. Each bullet — visible or revealed
                                // — still routes through the `li` override below,
                                // so the 💬 discuss handoff is unchanged.
                                ul: ({ children }) => {
                                  // remark inserts whitespace text nodes between
                                  // <li>s; drop them so the slice counts real
                                  // bullets, not newlines.
                                  const items = Children.toArray(children).filter(
                                    (c) => typeof c !== "string" || c.trim() !== "",
                                  );
                                  const head = items.slice(0, NARRATIVE_HEAD_BULLETS);
                                  const tail = items.slice(NARRATIVE_HEAD_BULLETS);
                                  return (
                                    <ul className="list-none pl-0 my-1 space-y-0.5">
                                      {head}
                                      {tail.length > 0 && (
                                        <li className="list-none">
                                          <details className="group mt-0.5">
                                            <summary className="flex items-center gap-1.5 py-0.5 cursor-pointer list-none text-xs text-fg-muted hover:text-fg transition-colors focus:outline-none focus:ring-1 focus:ring-indigo-500/40 rounded">
                                              <span aria-hidden className="text-[10px] transition-transform group-open:rotate-90">▸</span>
                                              <span>Show {tail.length} more signal{tail.length === 1 ? "" : "s"}</span>
                                            </summary>
                                            <ul className="list-none pl-0 mt-1 space-y-0.5">{tail}</ul>
                                          </details>
                                        </li>
                                      )}
                                    </ul>
                                  );
                                },
                                // Each narrative bullet is its own click target →
                                // hands off to chat to discuss that item, reusing
                                // the same 💬 "Discuss" affordance as the proposal
                                // cards. The bottom-line and "Move today:" lines are
                                // paragraphs/strong text, so they stay non-interactive.
                                li: ({ node, children }) => {
                                  const text = nodeToPlainText(node).trim();
                                  // No extractable text (empty bullet, or a
                                  // react-markdown version that doesn't forward
                                  // `node`) → render a plain, non-clickable item
                                  // rather than a button that seeds an empty prompt.
                                  if (!text) return <li className="list-none">{children}</li>;
                                  return (
                                    <li className="list-none">
                                      <button
                                        type="button"
                                        onClick={() => onContinue(buildNarrativeSeed(text))}
                                        aria-label={`Discuss: ${text}`}
                                        className="group flex w-full items-start gap-2 text-left cursor-pointer rounded -mx-1.5 px-1.5 py-0.5 transition hover:bg-indigo-500/10 focus:outline-none focus:ring-1 focus:ring-indigo-500/40"
                                      >
                                        {/* 💬 stands in for the bullet and signals
                                            "click to discuss"; items-start keeps it on
                                            the first line with the text hanging beside it.
                                            opacity lifts on hover as the click cue. */}
                                        <span aria-hidden className="mt-0.5 flex-shrink-0 select-none opacity-70 transition group-hover:opacity-100">💬</span>
                                        <span className="min-w-0">{children}</span>
                                      </button>
                                    </li>
                                  );
                                },
                              }
                            : undefined
                        }
                      >
                        {today.narrative}
                      </ReactMarkdown>
                    </div>
                  </div>
                </section>
              )}

              {isQuiet && activeDepts.length > 0 && (
                <div className="mb-6 flex items-center justify-between rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3">
                  <span className="text-sm text-emerald-300">Quiet day — nothing needs your attention.</span>
                  <Link href="/departments" className="text-xs text-indigo-400 hover:text-indigo-300 flex-shrink-0">
                    Set up a check-in →
                  </Link>
                </div>
              )}

              {/* Pulse card system — a symmetric two-column grid beneath the
                  narrative. Stacks to one column below xl, so each section
                  renders exactly once at every breakpoint (no mobile/desktop
                  duplicates). Left = action queue; right = awareness. */}
              <div className="grid gap-8 xl:grid-cols-[1.05fr_0.95fr]">
                {/* LEFT — action queue */}
                <div className="min-w-0 space-y-8">
                  {/* Needs you — decisions routed to the caller. Primary
                      section: visually dominant so the eye lands here first.
                      Buckets (mineProposals / otherProposals / monitoring)
                      are computed once above so the status strip and these
                      lists stay in sync. */}
                  <section id="sec-needs-you" className="rounded-xl border border-line bg-surface-elevated p-4">
                    <details open className="group">
                      <summary className="flex items-center gap-2 mb-3 cursor-pointer list-none border-l-2 border-indigo-500 pl-2">
                        <span className="text-[10px] text-fg-muted transition-transform group-open:rotate-90">▸</span>
                        <SectionLabel variant="primary" count={mineProposals.length}>
                          Needs you
                        </SectionLabel>
                      </summary>
                      {startHereProposal ? (
                        // Full queue inside a fixed-height scroll (like the Pulse
                        // Recent activity card) — no cap, no "Show more"; the list
                        // scrolls internally instead of growing the page.
                        <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
                          {/* Start here — the single sharpest item, emphasized
                              (indigo left accent + label) and expanded so there's
                              one obvious first move instead of a flat queue. The
                              ProposalCard supplies the row's own py-3, so this
                              wrapper adds none (avoids a double pad that would
                              break the divider-row rhythm). */}
                          <div>
                            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-indigo-300">
                              Start here
                            </div>
                            <ProposalCard
                              proposal={startHereProposal}
                              people={today.people}
                              onContinue={onContinue}
                              onApprove={handleApprove}
                              onDismiss={handleDismiss}
                              onApproveWithEdits={handleApproveWithEdits}
                              defaultBodyExpanded
                              emphasized
                            />
                          </div>
                          {(showAllNeedsYou ? restProposals : restProposals.slice(0, NEEDS_YOU_VISIBLE)).map((p) => (
                            <ProposalCard
                              key={p.alert_id}
                              proposal={p}
                              people={today.people}
                              onContinue={onContinue}
                              onApprove={handleApprove}
                              onDismiss={handleDismiss}
                              onApproveWithEdits={handleApproveWithEdits}
                            />
                          ))}
                          {restProposals.length > NEEDS_YOU_VISIBLE && (
                            <button
                              type="button"
                              onClick={() => setShowAllNeedsYou((v) => !v)}
                              className="w-full py-2 text-[11px] text-fg-muted hover:text-fg transition-colors"
                            >
                              {showAllNeedsYou
                                ? "Show fewer"
                                : `Show ${restProposals.length - NEEDS_YOU_VISIBLE} more`}
                            </button>
                          )}
                        </div>
                      ) : !isQuiet ? (
                        <p className="text-sm text-fg-muted py-3">Nothing waiting on you.</p>
                      ) : null}
                      {/* Relief valves: clear the old tail in one click, or ask
                          the Executive to re-judge everything right now. */}
                      {(staleNeedsYouIds.length > 0 || mineProposals.length > 0) && (
                        <div className="mt-2 flex flex-wrap items-center gap-3">
                          {staleNeedsYouIds.length > 0 && (
                            <button
                              type="button"
                              onClick={() => handleBulkDismiss(staleNeedsYouIds)}
                              className="text-[11px] text-fg-muted hover:text-rose-300 transition-colors"
                            >
                              ✕ Dismiss {staleNeedsYouIds.length} older than {NEEDS_YOU_DISMISS_OLDER_THAN_DAYS} days
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={handleRecheck}
                            disabled={recheckBusy}
                            className="text-[11px] text-fg-muted hover:text-indigo-300 transition-colors disabled:opacity-50"
                          >
                            {recheckBusy ? "⟳ Re-checking…" : "⟳ Re-check relevance"}
                          </button>
                        </div>
                      )}
                    </details>
                  </section>

                  {otherProposals.length > 0 && (
                    <section className="rounded-xl border border-line bg-surface-elevated p-4">
                      <SectionHeading title="Across the team" count={otherProposals.length} />
                      <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
                        {otherProposals.map((p) => (
                          <ProposalCard
                            key={p.alert_id}
                            proposal={p}
                            people={today.people}
                            onContinue={onContinue}
                            onApprove={handleApprove}
                            onDismiss={handleDismiss}
                            onApproveWithEdits={handleApproveWithEdits}
                          />
                        ))}
                      </div>
                    </section>
                  )}
                </div>

                {/* RIGHT — awareness (org health + ambient signals) */}
                <div className="min-w-0 space-y-8">
                  {/* Departments — only those needing attention get a row;
                      healthy + inactive ones fold into one quiet toggle. */}
                  <section id="sec-departments" className="rounded-xl border border-line bg-surface-elevated p-4">
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center gap-1.5">
                        <SectionLabel variant="ambient">Departments</SectionLabel>
                        <InfoTip align="left">
                          <span className="text-amber-300">At risk</span> /{" "}
                          <span className="text-rose-300">off track</span> = goal
                          health. <span className="text-sky-300">Awaiting</span> =
                          items waiting on the department head.{" "}
                          <span className="text-fg">Inactive</span> = no goals or
                          check-ins set up yet.
                        </InfoTip>
                      </div>
                      <Link href="/departments" className="text-xs text-indigo-400 hover:text-indigo-300">
                        View all →
                      </Link>
                    </div>

                    {activeDepts.length === 0 && (
                      <div className="py-3">
                        <p className="text-sm text-fg-muted mb-2">
                          No department has a check-in set up yet.
                        </p>
                        <Link href="/departments" className="text-xs text-indigo-400 hover:text-indigo-300">
                          Pick one to activate →
                        </Link>
                      </div>
                    )}

                    {/* Attention departments (full, no cap) + the quiet toggle
                        all share one fixed-height scroll, like the Pulse cards —
                        the section never grows the page. */}
                    {(attentionDepts.length > 0 || quietDeptCount > 0) && (
                      <div className="max-h-[32rem] overflow-y-auto pr-1">
                        {attentionDepts.length > 0 && (
                          <div className="divide-y divide-line">
                            {attentionDepts.map((d) => (
                              <DeptCard key={d.slug} dept={d} onContinue={onContinue} />
                            ))}
                          </div>
                        )}

                        {/* One quiet toggle for the rest — on-track and inactive
                            departments revealed together in a single column. */}
                        {quietDeptCount > 0 && (
                          <div className={attentionDepts.length > 0 ? "mt-3" : ""}>
                            <button
                              type="button"
                              onClick={() => setShowAllDeptsOpen((v) => !v)}
                              aria-expanded={showAllDeptsOpen}
                              className="flex items-center gap-1.5 min-h-[44px] text-xs text-fg-muted hover:text-fg transition-colors"
                            >
                              <span aria-hidden="true">{showAllDeptsOpen ? "▲" : "▼"}</span>
                              {quietDeptSummary}
                            </button>
                            {showAllDeptsOpen && (
                              <div className="divide-y divide-line mt-3">
                                {onTrackDepts.map((d) => (
                                  <DeptCard key={d.slug} dept={d} onContinue={onContinue} />
                                ))}
                                {inactiveDepts.map((d) => (
                                  <DeptCard key={d.slug} dept={d} onContinue={onContinue} dimmed />
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </section>

                  {/* People — full roster, scrolling inside the card. */}
                  {showPeopleSidebar && (
                    <section id="sec-people" className="rounded-xl border border-line bg-surface-elevated p-4">
                      {(() => {
                        const summary = peopleSummary(today.people);
                        return (
                          <div className="flex items-start justify-between gap-2 mb-3">
                            <div>
                              <h3 className="text-sm font-semibold text-fg">People</h3>
                              <p className={`text-xs mt-0.5 ${summary.hasOverdue ? "text-rose-300" : "text-fg-muted"}`}>
                                {summary.text}
                              </p>
                            </div>
                            <Link href="/people" className="flex-shrink-0 text-xs text-indigo-400 hover:text-indigo-300">View all</Link>
                          </div>
                        );
                      })()}
                      <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
                        {today.people.map((p) => (
                          <PersonRow key={p.id} person={p} />
                        ))}
                      </div>
                    </section>
                  )}

                  {/* Ambient — In flight (commitments with run times) and
                      Monitoring (live signals). Both render once; each is its
                      own Pulse card. */}
                  <InFlightPanel inFlight={today.in_flight ?? []} id={SECTION_IDS.inFlight} />
                  <PracticeClientsPanel
                    clients={today.practice_clients ?? []}
                    id={SECTION_IDS.practice}
                  />
                  <HandledOvernightPanel
                    items={handledOvernight}
                    onReopen={handleReopen}
                    undone={undoneRows}
                  />
                  <MonitoringPanel
                    proposals={monitoringProposals}
                    onContinue={onContinue}
                    onDismiss={handleDismiss}
                    onBulkDismiss={handleBulkDismiss}
                    id={SECTION_IDS.monitoring}
                  />
                </div>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
