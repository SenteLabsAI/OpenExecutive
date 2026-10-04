"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ackAlert,
  approveDecision,
  bulkAckAlerts,
  getToday,
  rejectDecision,
  reopenAlert,
  reviewAlerts,
  type ProposalItem,
  type Today,
} from "@/lib/api";
import { MEMORY_ACTIONS, briefingMemoryLine } from "@/lib/briefing-memory";
import { briefingSummary, greeting, type SummaryPart } from "@/lib/briefingSummary";
import { principalIdOf } from "@/lib/dueSoon";
import { groupHandled } from "@/lib/handled";
import {
  BRIEFING_REFRESH_INTERVAL_MS,
  narrativeRepollDelay,
  narrativeUpdatedLabel,
  shouldRefreshOnFocus,
} from "@/lib/narrativeFreshness";
import { reviewRanLabel } from "@/lib/rhythmCards";
import InfoTip from "./InfoTip";
import { REPLIES_WAITING_TIP, ReplyCardItem, useReplyCards } from "./RepliesWaiting";
import Icon from "./Icon";
import {
  HandledPanelBody,
  InFlightPanelBody,
  MonitoringPanelBody,
  PracticeClientsPanelBody,
  inFlightNext,
} from "./briefing/AwarenessPanels";
import NarrativeBody from "./briefing/Narrative";
import ProposalCard from "./briefing/ProposalCard";
import {
  NEEDS_YOU_DISMISS_OLDER_THAN_DAYS,
  TONE_TEXT,
  olderThan,
  type ContinueHandler,
} from "./briefing/shared";
import {
  DueSoonPanelBody,
  ProjectsPanelBody,
  TopThreeList,
  WeeklyReviewPanelBody,
  useActiveProjects,
  useOpenLoops,
  useWeeklyReview,
} from "./briefing/SoloPanels";
import { DepartmentsPanelBody, PeoplePanelBody, peopleSummary } from "./briefing/TeamPanels";
import { buttonClass } from "./ui/Button";
import OverflowMenu from "./ui/OverflowMenu";
import SidePanel from "./ui/SidePanel";
import { useWorkspace } from "./workspace/WorkspaceContext";

// The card pieces stay importable from here for pages that show them alone.
export { ProposalCard };
export { DeptCard } from "./briefing/TeamPanels";

// Home, the briefing: a greeting and one summary line, the Ask box, the
// "Needs you" queue as large cards, then a row of count tiles — one per
// awareness section with something in it — each opening its full list in a
// side panel. Today's written brief ("What's going on") opens in a panel too.

// "Needs you" shows this many cards before a "Show N more" toggle.
const NEEDS_YOU_VISIBLE = 3;

const NEEDS_YOU_ID = "sec-needs-you";

type PanelKey =
  | "brief"
  | "departments"
  | "people"
  | "inFlight"
  | "handled"
  | "monitoring"
  | "team"
  | "clients"
  | "projects"
  | "dueSoon"
  | "weeklyReview";

interface Tile {
  key: PanelKey;
  value: string;
  label: string;
  sub: string;
  subTone?: keyof typeof TONE_TEXT;
}

interface BriefingProps {
  // Called when the user clicks a briefing item to continue the thread
  // in chat. The parent (the root page) switches its main view from
  // briefing → chat and seeds the input with `prompt`. When omitted, items
  // render as plain navigation links.
  onContinue?: ContinueHandler;
  // Set to true when this Briefing is the root landing surface — adds the
  // greeting header.
  showHeader?: boolean;
  // Personalises the greeting when showHeader is true.
  firstName?: string;
  // Home's one banner slot: the most important notice the page has (paused,
  // no profile, a brief that wasn't sent). When empty, the quiet-day note
  // takes the slot.
  banner?: React.ReactNode;
}

export default function Briefing({ onContinue, showHeader = false, firstName, banner }: BriefingProps) {
  // Solo (one person, just for themselves): no departments, roster or
  // cross-team lanes — see the `solo` branches below.
  const { mode } = useWorkspace();
  const solo = mode === "solo";
  const [today, setToday] = useState<Today | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Alerts the user has acted on this session. We optimistically drop
  // them from the rendered lists so the click feels instant; the canonical
  // state is picked up by the next /today fetch.
  const [actedAlertIds, setActedAlertIds] = useState<Set<number>>(new Set());
  // "Needs you" shows NEEDS_YOU_VISIBLE cards; the rest sit behind one toggle.
  const [showAllNeedsYou, setShowAllNeedsYou] = useState(false);
  // "Re-check relevance" runs the Executive's review on demand.
  const [recheckBusy, setRecheckBusy] = useState(false);
  // Handled-rail rows undone this session (keyed per row, see handledKey).
  const [undoneRows, setUndoneRows] = useState<Set<string>>(new Set());
  // Which tile's side panel (or the brief) is open.
  const [openPanel, setOpenPanel] = useState<PanelKey | null>(null);
  const [askText, setAskText] = useState("");

  // Re-pull /today after a server-side mutation (e.g. a decision approve/reject)
  // so derived data — the narrative, per-person and department counts —
  // re-syncs. The optimistic actedAlertIds set already hides the card; this
  // refreshes everything computed from it. Best-effort: a failed refresh leaves
  // the stale-but-still-usable view rather than erroring the briefing.
  const [mountedAt] = useState(() => Date.now());
  const lastFetchRef = useRef(mountedAt);
  const refreshToday = useCallback(() => {
    lastFetchRef.current = Date.now();
    getToday().then(setToday).catch(() => { /* keep current view on failure */ });
  }, []);

  // Keep "What's going on" current. /today serves the cached header and
  // rewrites it in the background when the picture moved (narrative_stale);
  // re-poll a few times so the new one lands without a reload.
  // Keyed on the whole response: a poll that comes back still stale is a new
  // object with the same fields, and must schedule the next attempt.
  const repollAttemptRef = useRef(0);
  useEffect(() => {
    if (!today?.narrative_stale) {
      repollAttemptRef.current = 0;
      return;
    }
    const delay = narrativeRepollDelay(repollAttemptRef.current);
    if (delay === null) return;
    const timer = setTimeout(() => {
      repollAttemptRef.current += 1;
      refreshToday();
    }, delay);
    return () => clearTimeout(timer);
  }, [today, refreshToday]);

  // …and when the tab comes back, and every few minutes while it is open.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && shouldRefreshOnFocus(lastFetchRef.current, Date.now())) {
        repollAttemptRef.current = 0;
        refreshToday();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") refreshToday();
    }, BRIEFING_REFRESH_INTERVAL_MS);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(interval);
    };
  }, [refreshToday]);

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
          briefingMemoryLine(MEMORY_ACTIONS.approve, proposal.headline),
        );
      }
    } catch (e) {
      // Revert the optimistic removal on failure so the user can retry.
      setActedAlertIds(prev);
      console.error("Approve failed", e);
    }
  }, [actedAlertIds, onContinue, refreshToday]);

  // Dismiss is a record-and-forget decision: the card disappears
  // immediately, the backend marks the alert ``dismissed`` (so the
  // Executive stops re-surfacing the same thread), and the user stays on
  // the briefing — no chat turn is seeded. On failure we revert the
  // optimistic removal so the user can see the card came back and retry.
  const handleDismiss = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      // A roster request was already answered by its own card (which also
      // clears the companion alert): only hide it and re-sync.
      if (proposal.roster_request) {
        refreshToday();
        return;
      }
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
      console.error("Dismiss failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Bulk dismiss: sends explicit ids (the cards the caller can see), never a
  // server-side age sweep. Same optimistic-removal + rollback pattern as the
  // single-card handlers.
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
      console.error("Alert review failed", e);
    } finally {
      setRecheckBusy(false);
    }
  }, [refreshToday]);

  // Undo an autonomous close from the Handled panel. The rail is rebuilt
  // from the audit log on every fetch (the "closed" row persists after a
  // reopen), so a 409 "already open" after a reload counts as done.
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
          briefingMemoryLine(MEMORY_ACTIONS.approveWithEdits, proposal.headline),
        );
      }
    } catch (e) {
      setActedAlertIds(prev);
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

  // Replies the Executive drafted in the owner's own mailbox (Act as me):
  // cards in Needs you. Empty for everyone else.
  const replies = useReplyCards();
  // Solo's own sections load on their own, so their tiles can count them.
  const principalId = solo ? principalIdOf(today?.people ?? []) : null;
  const projects = useActiveProjects(solo);
  const openLoops = useOpenLoops(principalId);
  const weeklyReview = useWeeklyReview(solo);

  // Hand an Ask-box question to chat as the user's own words.
  const submitAsk = () => {
    const text = askText.trim();
    if (!text || !onContinue) return;
    onContinue(text);
  };

  // Jump from the Handled panel to the alert's card; when it is folded
  // behind "Show more" open the queue first, and land on the queue when the
  // card isn't on the page at all.
  const jumpToAlert = (alertId: number) => {
    setOpenPanel(null);
    setShowAllNeedsYou(true);
    setTimeout(() => {
      const target = document.getElementById(`alert-${alertId}`) ?? document.getElementById(NEEDS_YOU_ID);
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 120);
  };

  const dateLabel = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });

  const departments = today?.departments ?? [];
  const activeDepts = departments.filter((d) => d.goal_count > 0 || d.awaiting_count > 0);
  // Only the departments with a problem (at risk / off track / awaiting)
  // count as needing attention; healthy ones fold into a quiet toggle in
  // the panel.
  const attentionDepts = activeDepts.filter(
    (d) => d.at_risk_count > 0 || d.off_track_count > 0 || d.awaiting_count > 0,
  );

  const isQuiet =
    today !== null &&
    today.proposals.length === 0 &&
    replies.cards.length === 0 &&
    activeDepts.every((d) => d.at_risk_count === 0 && d.off_track_count === 0 && d.awaiting_count === 0) &&
    today.people.every((p) => p.awaiting_count === 0);

  const showPeople = !solo && (today?.people.length ?? 0) > 1;

  // Bucket proposals once so the summary line, the queue and the tiles work
  // off the same split. "Needs you" = action proposals routed to the caller,
  // plus unrouted catch-all items when the caller is the principal. When the
  // caller can't be resolved (caller_person_id null — e.g. a direct curl),
  // fall back to the legacy single bucket so the queue still renders.
  // Optimistically-acted alerts are dropped first so counts reflect the click.
  const callerId = today?.caller_person_id ?? null;
  const isPrincipalCaller =
    callerId == null ? false : today?.people.find((p) => p.id === callerId)?.is_principal ?? false;
  const liveProposals = (today?.proposals ?? []).filter((p) => !actedAlertIds.has(p.alert_id));
  const monitoringProposals = liveProposals.filter((p) => p.category === "monitoring");
  const actionProposals = liveProposals.filter((p) => p.category !== "monitoring");
  // Solo: everything is yours, so nothing goes to an "Across the team" lane.
  const mineProposals =
    callerId == null || solo
      ? actionProposals
      : actionProposals.filter(
          (p) => p.routed_to_person_id === callerId || (p.routed_to_person_id == null && isPrincipalCaller),
        );
  const mineIds = new Set(mineProposals.map((p) => p.alert_id));
  const otherProposals =
    callerId == null || solo ? [] : actionProposals.filter((p) => !mineIds.has(p.alert_id));
  // A roster request is answered on its card, never swept (the server skips
  // it too), so it is not counted here.
  const staleNeedsYouIds = olderThan(
    mineProposals.filter((p) => !p.roster_request),
    NEEDS_YOU_DISMISS_OLDER_THAN_DAYS,
  );
  const handledOvernight = today?.handled_overnight ?? [];
  const handledRows = groupHandled(handledOvernight);
  const inFlight = today?.in_flight ?? [];
  const practiceClients = today?.practice_clients ?? [];

  // Summary-line inputs. Solo has no Departments or People tiles, so it says
  // how many goals need attention instead, linking to /goals.
  const deptAtRiskCount = solo
    ? 0
    : attentionDepts.filter((d) => d.at_risk_count > 0 || d.off_track_count > 0).length;
  const peopleNeedReply = solo ? 0 : (today?.people ?? []).filter((p) => p.status === "needs_reply").length;
  const peopleOverdue = solo ? 0 : (today?.people ?? []).filter((p) => p.overdue).length;
  const goalsAtRisk = solo ? departments.reduce((n, d) => n + d.at_risk_count + d.off_track_count, 0) : 0;
  // The top three words a commitment as the owner's ("you").
  const principalName =
    (principalId != null && today?.people.find((p) => p.id === principalId)?.full_name) || "";
  const needsYouCount = mineProposals.length + replies.cards.length;
  const summary: SummaryPart[] = today
    ? briefingSummary({
        needsYou: needsYouCount,
        handledOvernight: handledRows.length,
        peopleOverdue,
        peopleNeedReply,
        deptAtRisk: deptAtRiskCount,
        goalsAtRisk,
        inFlight: inFlight.length,
        monitoring: monitoringProposals.length,
      })
    : [];

  // The count tiles — only those with something to show.
  const tiles: Tile[] = [];
  if (today) {
    if (solo) {
      const view = openLoops.view;
      if (principalId != null && (openLoops.error || (view && (view.items.length > 0 || view.later > 0)))) {
        const overdue = view?.items.filter((i) => i.overdue).length ?? 0;
        tiles.push({
          key: "dueSoon",
          value: String(view?.items.length ?? "–"),
          label: "Due soon",
          sub: overdue > 0 ? `${overdue} overdue` : view && view.items.length > 0 ? "this week" : `${view?.later ?? 0} later`,
          subTone: overdue > 0 ? "amber" : undefined,
        });
      }
      if (projects.error || (projects.projects && projects.projects.length > 0)) {
        tiles.push({
          key: "projects",
          value: String(projects.projects?.length ?? "–"),
          label: "Your projects",
          sub: "active",
        });
      }
      if (weeklyReview) {
        tiles.push({
          key: "weeklyReview",
          value: "Ready",
          label: "This week's review",
          sub: reviewRanLabel(weeklyReview.completed_at) || weeklyReview.period || "",
        });
      }
    } else {
      const deptValue = attentionDepts.length > 0 ? attentionDepts.length : activeDepts.length || departments.length;
      tiles.push({
        key: "departments",
        value: String(deptValue),
        label: "Departments",
        sub:
          attentionDepts.length > 0
            ? deptAtRiskCount > 0
              ? "at risk"
              : "need attention"
            : activeDepts.length > 0
              ? "on track"
              : "not set up yet",
        subTone: attentionDepts.length > 0 ? "amber" : activeDepts.length > 0 ? "emerald" : undefined,
      });
      if (showPeople && today) {
        const people = today.people;
        const busy = people.filter((p) => p.status === "needs_reply" || p.status === "awaiting").length;
        const s = peopleSummary(people);
        tiles.push({
          key: "people",
          value: String(busy > 0 ? busy : people.length),
          label: "People",
          sub: busy > 0 ? s.text : "all clear",
          subTone: s.hasOverdue ? "rose" : busy > 0 ? "amber" : "emerald",
        });
      }
    }
    if (otherProposals.length > 0)
      tiles.push({ key: "team", value: String(otherProposals.length), label: "Across the team", sub: "with teammates" });
    if (inFlight.length > 0)
      tiles.push({
        key: "inFlight",
        value: String(inFlight.length),
        label: "In flight",
        sub: inFlightNext(inFlight),
        subTone: inFlight.some((f) => f.overdue) ? "amber" : undefined,
      });
    if (handledRows.length > 0)
      tiles.push({
        key: "handled",
        value: String(handledRows.length),
        label: "Handled overnight",
        sub: "by the Executive",
        subTone: "emerald",
      });
    if (monitoringProposals.length > 0)
      tiles.push({
        key: "monitoring",
        value: String(monitoringProposals.length),
        label: "Monitoring",
        sub: monitoringProposals.length === 1 ? "signal" : "signals",
      });
    if (practiceClients.length > 0)
      tiles.push({
        key: "clients",
        value: String(practiceClients.length),
        label: "Across your clients",
        sub: "parked clients",
      });
  }

  // The Needs you queue: replies waiting first (they came in first, as their
  // own section did), then the proposals in score order.
  type QueueItem = { key: string; render: (emphasized: boolean) => React.ReactNode };
  const queue: QueueItem[] = [
    ...replies.cards.map((card) => ({
      key: `reply-${card.decision_id}`,
      render: (emphasized: boolean) => (
        <ReplyCardItem card={card} onGone={replies.gone} onRefresh={replies.refresh} emphasized={emphasized} />
      ),
    })),
    ...mineProposals.map((p) => ({
      key: `alert-${p.alert_id}`,
      render: (emphasized: boolean) => (
        <ProposalCard
          proposal={p}
          people={today?.people ?? []}
          onContinue={onContinue}
          onApprove={handleApprove}
          onDismiss={handleDismiss}
          onApproveWithEdits={handleApproveWithEdits}
          defaultBodyExpanded={emphasized}
          emphasized={emphasized}
        />
      ),
    })),
  ];
  const visibleQueue = showAllNeedsYou ? queue : queue.slice(0, NEEDS_YOU_VISIBLE);
  const hiddenCount = queue.length - visibleQueue.length;

  const needsYouMenu = [
    ...(staleNeedsYouIds.length > 0
      ? [
          {
            label: `Dismiss ${staleNeedsYouIds.length} older than ${NEEDS_YOU_DISMISS_OLDER_THAN_DAYS} days`,
            onSelect: () => void handleBulkDismiss(staleNeedsYouIds),
          },
        ]
      : []),
    {
      label: recheckBusy ? "Re-checking…" : "Re-check relevance",
      onSelect: () => void handleRecheck(),
      disabled: recheckBusy,
    },
  ];
  const showNeedsYouMenu = staleNeedsYouIds.length > 0 || mineProposals.length > 0;

  const hasBrief = Boolean(today && (today.narrative || today.narrative_stale));
  const briefFreshness = today
    ? today.narrative_stale
      ? "Refreshing…"
      : narrativeUpdatedLabel(today.narrative_generated_at, new Date())
    : null;

  const quietBanner =
    isQuiet && activeDepts.length > 0 ? (
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3">
        <span className={`text-[15px] ${TONE_TEXT.emerald}`}>Quiet day — nothing needs your attention.</span>
        {!solo && (
          <Link href="/departments" className="text-sm font-medium text-accent hover:underline">
            Set up a check-in →
          </Link>
        )}
      </div>
    ) : null;
  const bannerSlot = banner ?? quietBanner;

  const onSummaryPart = (part: SummaryPart) => {
    if (part.target.kind === "needsYou") {
      document.getElementById(NEEDS_YOU_ID)?.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (part.target.kind === "panel") {
      setOpenPanel(part.target.panel);
    }
  };

  const panelFooterLink = (href: string, label: string) => (
    <Link href={href} className={buttonClass("secondary", "md", "w-full sm:w-auto")}>
      {label}
    </Link>
  );

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6 sm:py-10">
          <header className="mb-6">
            <p className="text-sm font-medium text-fg-muted">{dateLabel}</p>
            <h1 className="mt-1 text-2xl sm:text-3xl font-bold tracking-tight text-fg">
              {showHeader ? greeting(firstName) : "Today"}
            </h1>
            {today && (
              <p className="mt-2 text-base sm:text-lg text-fg-muted leading-snug">
                {summary.length === 0
                  ? "All clear — nothing needs you right now."
                  : summary.map((part, i) => (
                      <span key={part.text}>
                        {part.target.kind === "href" ? (
                          <Link href={part.target.href} className="hover:text-fg hover:underline underline-offset-4">
                            {part.text}
                          </Link>
                        ) : (
                          <button
                            type="button"
                            onClick={() => onSummaryPart(part)}
                            className="cursor-pointer text-left hover:text-fg hover:underline underline-offset-4"
                          >
                            {part.text}
                          </button>
                        )}
                        {i < summary.length - 1 ? ". " : "."}
                      </span>
                    ))}
              </p>
            )}
          </header>

          {onContinue && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                submitAsk();
              }}
              className="mb-6 flex items-center gap-2 rounded-2xl border border-line bg-surface-elevated p-2 pl-4 shadow-sm focus-within:border-accent/60 focus-within:ring-2 focus-within:ring-accent/20"
            >
              <input
                type="text"
                value={askText}
                onChange={(e) => setAskText(e.target.value)}
                placeholder="Ask your Executive anything…"
                aria-label="Ask your Executive"
                className="min-w-0 flex-1 bg-transparent py-2 text-base text-fg placeholder:text-fg-muted focus:outline-none"
              />
              <button
                type="submit"
                disabled={!askText.trim()}
                aria-label="Ask"
                className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-xl bg-accent-strong text-white transition-colors hover:bg-accent-strong/90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Icon name="arrow-send" size="w-4 h-4" />
              </button>
            </form>
          )}

          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-[15px] text-rose-700 dark:text-rose-300">
              {error}
            </div>
          )}

          {bannerSlot && <div className="mb-6">{bannerSlot}</div>}

          {today && (
            <>
              {/* Needs you — decisions routed to the caller, and replies
                  waiting to be sent. */}
              <section id={NEEDS_YOU_ID} className="mb-8 scroll-mt-4">
                <div className="mb-3 flex items-center gap-2">
                  <h2 className="text-lg font-semibold text-fg">
                    Needs you
                    {needsYouCount > 0 && <span className="ml-1.5 font-normal text-fg-muted">({needsYouCount})</span>}
                  </h2>
                  {replies.cards.length > 0 && <InfoTip align="left">{REPLIES_WAITING_TIP}</InfoTip>}
                  <div className="ml-auto flex items-center gap-1">
                    {hasBrief && (
                      <button
                        type="button"
                        onClick={() => setOpenPanel("brief")}
                        className={`min-h-[40px] rounded-xl px-3 text-sm font-medium text-accent hover:bg-accent/10 transition-colors${
                          !today.narrative ? " animate-pulse" : ""
                        }`}
                      >
                        {today.narrative ? "Read today's brief" : "Catching up on today…"}
                      </button>
                    )}
                    {showNeedsYouMenu && <OverflowMenu size="sm" label="Needs you options" items={needsYouMenu} />}
                  </div>
                </div>
                {replies.notice && (
                  <p className={`mb-3 text-sm ${TONE_TEXT.emerald}`} role="status">
                    {replies.notice}
                  </p>
                )}
                {queue.length > 0 ? (
                  <div className="space-y-4">
                    {visibleQueue.map((item, i) => (
                      <div key={item.key}>{item.render(i === 0)}</div>
                    ))}
                    {(hiddenCount > 0 || (showAllNeedsYou && queue.length > NEEDS_YOU_VISIBLE)) && (
                      <div className="flex justify-center">
                        <button
                          type="button"
                          onClick={() => setShowAllNeedsYou((v) => !v)}
                          className={buttonClass("secondary", "md")}
                        >
                          {showAllNeedsYou ? "Show fewer" : `Show ${hiddenCount} more`}
                        </button>
                      </div>
                    )}
                  </div>
                ) : !isQuiet ? (
                  <p className="rounded-2xl border border-dashed border-line px-4 py-5 text-[15px] text-fg-muted">
                    Nothing waiting on you.
                  </p>
                ) : null}
              </section>

              {solo && (
                <div className="mb-8">
                  <TopThreeList ownerName={principalName} id="sec-top-three" />
                </div>
              )}

              {tiles.length > 0 && (
                <section aria-labelledby="sec-everything-else" className="mb-8">
                  <h2 id="sec-everything-else" className="mb-3 text-lg font-semibold text-fg">
                    Everything else
                  </h2>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                    {tiles.map((t) => (
                      <button
                        key={t.key}
                        type="button"
                        onClick={() => setOpenPanel(t.key)}
                        aria-haspopup="dialog"
                        className="flex min-h-[7.5rem] flex-col items-start rounded-2xl border border-line bg-surface-elevated p-4 text-left transition-colors hover:border-line-strong hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                      >
                        <span className="text-2xl sm:text-3xl font-bold tracking-tight text-fg tabular-nums">
                          {t.value}
                        </span>
                        <span className="mt-1 text-[15px] font-medium text-fg">{t.label}</span>
                        {t.sub && (
                          <span className={`mt-0.5 text-sm ${t.subTone ? TONE_TEXT[t.subTone] : "text-fg-muted"}`}>
                            {t.sub}
                          </span>
                        )}
                      </button>
                    ))}
                  </div>
                </section>
              )}

              <SidePanel
                open={openPanel === "brief"}
                onClose={() => setOpenPanel(null)}
                title="Today's brief"
                subtitle={<span aria-live="polite">{briefFreshness ?? dateLabel}</span>}
                width="lg"
              >
                <NarrativeBody
                  narrative={today.narrative}
                  stale={today.narrative_stale ?? false}
                  solo={solo}
                  onContinue={onContinue}
                />
              </SidePanel>
              <SidePanel
                open={openPanel === "departments"}
                onClose={() => setOpenPanel(null)}
                title="Departments"
                width="lg"
                footer={panelFooterLink("/departments", "View all departments")}
              >
                <DepartmentsPanelBody departments={departments} onContinue={onContinue} />
              </SidePanel>
              <SidePanel
                open={openPanel === "people"}
                onClose={() => setOpenPanel(null)}
                title="People"
                width="lg"
                footer={panelFooterLink("/people", "View all people")}
              >
                <PeoplePanelBody people={today.people} />
              </SidePanel>
              <SidePanel
                open={openPanel === "team"}
                onClose={() => setOpenPanel(null)}
                title="Across the team"
                subtitle="Waiting on someone else"
                width="lg"
              >
                <div className="space-y-4">
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
                  {otherProposals.length === 0 && (
                    <p className="text-[15px] text-fg-muted">Nothing waiting on anyone else.</p>
                  )}
                </div>
              </SidePanel>
              <SidePanel open={openPanel === "inFlight"} onClose={() => setOpenPanel(null)} title="In flight">
                <InFlightPanelBody inFlight={inFlight} />
              </SidePanel>
              <SidePanel open={openPanel === "handled"} onClose={() => setOpenPanel(null)} title="Handled overnight">
                <HandledPanelBody
                  items={handledOvernight}
                  onReopen={handleReopen}
                  undone={undoneRows}
                  onJumpToAlert={jumpToAlert}
                />
              </SidePanel>
              <SidePanel open={openPanel === "monitoring"} onClose={() => setOpenPanel(null)} title="Monitoring">
                <MonitoringPanelBody
                  proposals={monitoringProposals}
                  onContinue={onContinue}
                  onDismiss={handleDismiss}
                  onBulkDismiss={handleBulkDismiss}
                />
              </SidePanel>
              <SidePanel
                open={openPanel === "clients"}
                onClose={() => setOpenPanel(null)}
                title="Across your clients"
                footer={panelFooterLink("/clients", "Manage clients")}
              >
                <PracticeClientsPanelBody clients={practiceClients} />
              </SidePanel>
              {solo && (
                <>
                  <SidePanel
                    open={openPanel === "projects"}
                    onClose={() => setOpenPanel(null)}
                    title="Your projects"
                    footer={panelFooterLink("/goals", "Goals")}
                  >
                    <ProjectsPanelBody state={projects} />
                  </SidePanel>
                  <SidePanel
                    open={openPanel === "dueSoon"}
                    onClose={() => setOpenPanel(null)}
                    title="Due soon"
                    footer={principalId != null ? panelFooterLink(`/people/${principalId}`, "All open items") : undefined}
                  >
                    <DueSoonPanelBody state={openLoops} />
                  </SidePanel>
                  {weeklyReview && (
                    <SidePanel
                      open={openPanel === "weeklyReview"}
                      onClose={() => setOpenPanel(null)}
                      title="This week's review"
                    >
                      <WeeklyReviewPanelBody review={weeklyReview} />
                    </SidePanel>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
