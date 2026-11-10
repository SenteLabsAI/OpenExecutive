"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ackAlert,
  answerAlert,
  approveAlert,
  approveDecision,
  bulkAckAlerts,
  dismissActionCard,
  dismissInviteCard,
  dismissReplyCard,
  dismissRightNowRow,
  getAlertApproval,
  getDelegation,
  getToday,
  rejectDecision,
  reopenAlert,
  rerunWorkflowRun,
  snoozeAlert,
  type DoneRow,
  type ProposalItem,
  type RightNowRow,
  type Today,
} from "@/lib/api";
import { greeting, homeSummary, type SummaryPart } from "@/lib/briefingSummary";
import { cardFromSearch } from "@/lib/cardMentions";
import { FIRST_RUN_LINKS } from "@/lib/firstRun";
import { principalIdOf } from "@/lib/dueSoon";
import {
  counted,
  departmentsText,
  goalTotals,
  goalsText,
  peopleText,
  realDepartments,
  tasksText,
  watchingText,
} from "@/lib/glance";
import { groupHandled } from "@/lib/handled";
import {
  NEEDS_YOU_OLDER_AFTER_DAYS,
  ageAnchor,
  clearAll,
  clearNotice,
  groupByRequest,
  newestAnchor,
  splitOlder,
  waitedOnFirst,
} from "@/lib/needsYou";
import { BRIEFING_REFRESH_INTERVAL_MS, shouldRefreshOnFocus } from "@/lib/narrativeFreshness";
import type { SnoozeChoice } from "@/lib/snooze";
import InfoTip from "./InfoTip";
import { ACTION_CARDS_TIP, ActionCardItem, useActionCards } from "./ActionCards";
import { INVITE_CARDS_TIP, InviteCardItem, useInviteCards } from "./InviteCards";
import { REPLIES_WAITING_TIP, ReplyCardItem, useReplyCards } from "./RepliesWaiting";
import Icon from "./Icon";
import {
  HandledPanelBody,
  InFlightPanelBody,
  MonitoringPanelBody,
  PracticeClientsPanelBody,
} from "./briefing/AwarenessPanels";
import DoneForYou, { DONE_FOR_YOU_ID } from "./briefing/DoneForYou";
import { anyWorking } from "@/lib/doneForYou";
import FirstWeekCard from "./briefing/FirstWeekCard";
import { useGlance } from "./briefing/Glance";
import { RightNowItem } from "./briefing/RightNow";
import WaitingElsewhereRow from "./briefing/WaitingElsewhere";
import WatchingNext from "./briefing/WatchingNext";
import { isHeldAction } from "./briefing/HeldActionCard";
import { isLeadTraining } from "./briefing/LeadTrainingCard";
import ProposalCard from "./briefing/ProposalCard";
import RequestGroupCard from "./briefing/RequestGroupCard";
import { TONE_TEXT, type ContinueHandler } from "./briefing/shared";
import {
  ProjectsPanelBody,
  WeeklyReviewPanelBody,
  useActiveProjects,
  useOpenLoops,
  useWeeklyReview,
} from "./briefing/SoloPanels";
import { DepartmentsPanelBody, PeoplePanelBody } from "./briefing/TeamPanels";
import { buttonClass } from "./ui/Button";
import SidePanel from "./ui/SidePanel";
import { useWorkspace } from "./workspace/WorkspaceContext";
import { PageSpinner } from "@/components/ui/Spinner";

// The card pieces stay importable from here for pages that show them alone.
export { ProposalCard };
export { DeptCard } from "./briefing/TeamPanels";

// Home, the briefing: a greeting and one summary line, the Ask box, the
// "Needs you" queue as large cards, then "At a glance": a row of count tiles —
// one per section with something in it — each opening its full list in a
// side panel or its page. Today's written brief ("What's going on") opens in a panel too.

// "Needs you" shows this many cards before a "Show N more" toggle.
const NEEDS_YOU_VISIBLE = 3;
// How often Home re-reads while something in Done for you is running, and
// for how long.
const DONE_POLL_MS = 8000;
const DONE_POLL_MAX_MS = 30 * 60 * 1000;

const NEEDS_YOU_ID = "sec-needs-you";

type PanelKey =
  | "departments"
  | "people"
  | "inFlight"
  | "handled"
  | "monitoring"
  | "team"
  | "clients"
  | "projects"
  | "weeklyReview";

interface Tile {
  key: string;
  // An item opens its side panel, or else goes to its page.
  panel?: PanelKey;
  href?: string;
  // What it says, whole ("5 goals, 1 at risk").
  text: string;
}

interface BriefingProps {
  // Called when the user clicks a briefing item to continue the thread
  // in chat. The parent (the root page) switches its main view from
  // briefing → chat and seeds the input with `prompt`. When omitted, items
  // render as plain navigation links.
  onContinue?: ContinueHandler;
  // Opens one of the reader's chats in place (Done for you's See / Watch).
  onOpenChat?: (sessionId: string, watch?: boolean) => void;
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

export default function Briefing({ onContinue, onOpenChat, showHeader = false, firstName, banner }: BriefingProps) {
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
  // Cards untouched for a few days fold into one "Older (N)" line.
  const [showOlder, setShowOlder] = useState(false);
  const [clearingOlder, setClearingOlder] = useState(false);
  const [olderNotice, setOlderNotice] = useState<string | null>(null);
  // The card just snoozed, for the "Snoozed until …" notice and its Undo.
  const [snoozed, setSnoozed] = useState<{ alertId: number; short: string } | null>(null);
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

  // Keep Home current when the tab comes back, and every few minutes while
  // it is open.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && shouldRefreshOnFocus(lastFetchRef.current, Date.now())) {
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

  // Approve carries the card out on the server. Decision-backed cards
  // (gated calendar bookings, held actions) go through /decisions, which
  // books or sends and clears the companion alert. Every other proposal goes
  // through /alerts/{id}/approve, which records who approved it, then queues
  // the action it held or runs the work as a chat on the server; a row in
  // Needs you follows that work until it is done.
  const handleApprove = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      if (proposal.decision_instance_id != null) {
        await approveDecision(proposal.decision_instance_id);
        refreshToday();  // re-sync narrative + counts (no chat nav to trigger it)
        return;
      }
      // Its progress shows in Done for you, read from the server.
      await approveAlert(proposal.alert_id);
      refreshToday();
    } catch (e) {
      // Revert the optimistic removal on failure so the user can retry.
      setActedAlertIds(prev);
      console.error("Approve failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // A card that asks something is answered on the card; the
  // server closes it and the Executive carries on in a new chat (its
  // progress shows in Done for you). Throws so the card can say why.
  const handleAnswer = useCallback(async (proposal: ProposalItem, answer: string) => {
    await answerAlert(proposal.alert_id, answer);
    setActedAlertIds((prev) => new Set(prev).add(proposal.alert_id));
    refreshToday();
  }, [refreshToday]);

  // Dismiss is a record-and-forget decision: the card disappears
  // immediately, the backend marks the alert ``dismissed`` (so the
  // Executive stops re-surfacing the same thread), and the user stays on
  // the briefing — no chat turn is seeded. On failure we revert the
  // optimistic removal so the user can see the card came back and retry.
  const handleDismiss = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      // A roster request, a Take the lead training card or a held chat
      // action was already answered on its own card (which also clears the
      // companion alert): only hide it and re-sync.
      if (proposal.roster_request || isLeadTraining(proposal) || isHeldAction(proposal)) {
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

  // Snooze: the card leaves now and the server brings it back at `until`.
  // Undo clears the snooze and shows the card again.
  const handleSnooze = useCallback(async (proposal: ProposalItem, choice: SnoozeChoice) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await snoozeAlert(proposal.alert_id, choice.until);
      setSnoozed({ alertId: proposal.alert_id, short: choice.short });
    } catch (e) {
      setActedAlertIds(prev);
      console.error("Snooze failed", e);
    }
  }, [actedAlertIds]);

  const handleUnsnooze = useCallback(async () => {
    if (!snoozed) return;
    try {
      await snoozeAlert(snoozed.alertId, null);
      setActedAlertIds((prev) => {
        const next = new Set(prev);
        next.delete(snoozed.alertId);
        return next;
      });
      setSnoozed(null);
      refreshToday();
    } catch (e) {
      console.error("Undo snooze failed", e);
    }
  }, [snoozed, refreshToday]);

  // Run again on a failed scheduled run: the server queues it and closes the
  // card, so the card leaves at once and comes back only if it fails again.
  const [rerunNotice, setRerunNotice] = useState<string | null>(null);
  const handleRerun = useCallback(async (proposal: ProposalItem) => {
    if (!proposal.failed_run_id) return;
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await rerunWorkflowRun(proposal.failed_run_id);
      setRerunNotice("Running it again now. The result goes where it normally would.");
      refreshToday();
    } catch (e) {
      setActedAlertIds(prev);
      setRerunNotice(e instanceof Error ? e.message : "Couldn't run it again.");
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

  // Approve-with-edits: the user tweaked the draft and wants exactly what
  // they wrote sent (no rephrasing). Same server-side approval as Approve,
  // with the edited text carried verbatim.
  const handleApproveWithEdits = useCallback(async (proposal: ProposalItem, editedBody: string) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await approveAlert(proposal.alert_id, editedBody);
      refreshToday();
    } catch (e) {
      setActedAlertIds(prev);
      console.error("Approve-with-edits failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Try again on an approval that didn't work (Done for you): the server
  // lets a card whose last approval failed be approved again, with the same
  // edits, and the row turns back to Working on it.
  const handleRetryApproval = useCallback(async (row: DoneRow) => {
    if (row.alert_id == null) return;
    try {
      const last = row.approval_id != null ? await getAlertApproval(row.approval_id) : null;
      await approveAlert(row.alert_id, last?.edited_body ?? undefined);
      refreshToday();
    } catch (e) {
      console.error("Retry approval failed", e);
    }
  }, [refreshToday]);

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
  // Actions it suggested from the caller's email, to approve (Act as me).
  const actionCards = useActionCards();
  // Meeting invites from people the caller knows, to accept or decline (Act as me).
  const inviteCards = useInviteCards();
  // Solo's own sections load on their own, so their tiles can count them.
  const principalId = solo ? principalIdOf(today?.people ?? []) : null;
  const projects = useActiveProjects(solo);
  // Due soon is the caller's own (the owner's, in solo).
  const loopOwnerId = solo ? principalId : (today?.caller_person_id ?? null);
  const openLoops = useOpenLoops(loopOwnerId);
  const glance = useGlance(today !== null);
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

  // A link in a message (`/?card=220`, lib/cardMentions.ts and the server's
  // alerts/card_refs.py) opens Home on that card, once the board has loaded.
  const cardLinkDone = useRef(false);
  useEffect(() => {
    if (cardLinkDone.current || loading || !today || typeof window === "undefined") return;
    cardLinkDone.current = true;
    const cardId = cardFromSearch(window.location.search);
    if (cardId === null) return;
    // After this render: the card may sit behind "Older" or "Show more".
    // No cleanup: a refresh of the board in the same tick must not cancel it.
    setTimeout(() => {
      setShowOlder(true);
      jumpToAlert(cardId);
    }, 0);
  }, [loading, today]);

  // What Right now's rows open: the email (and its reply card, shared with
  // Needs you). ✕ on a row clears it at once; the server keeps it out until
  // something newer lands on it.
  const onDismissRow = useCallback((row: RightNowRow) => {
    setToday((t) => t && { ...t, right_now: (t.right_now ?? []).filter((r) => r.key !== row.key) });
    dismissRightNowRow(row.key)
      .then((kept) => {
        if (!kept) return getToday().then(setToday);
      })
      .catch((e) => console.error("Dismissing a Right now row failed", e));
  }, []);
  const onRowGone = useCallback((row: RightNowRow) => {
    setToday((t) => t && { ...t, right_now: (t.right_now ?? []).filter((r) => r.key !== row.key) });
  }, []);
  const rightNowActions = { replies, onContinue, onDismiss: onDismissRow, onGone: onRowGone };

  const dateLabel = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });

  const departments = today?.departments ?? [];
  const activeDepts = departments.filter((d) => d.goal_count > 0 || d.awaiting_count > 0);
  // Only the departments with a problem (at risk / off track / awaiting)
  // count as needing attention; healthy ones fold into a quiet toggle in
  // the panel.
  const attentionDepts = activeDepts.filter(
    (d) => d.at_risk_count > 0 || d.off_track_count > 0 || d.awaiting_count > 0,
  );
  // The Departments tile counts only real departments, not the row that holds
  // the goals with no department.
  const activeRealDepts = realDepartments(activeDepts);
  const attentionRealDepts = realDepartments(attentionDepts);
  const goals = goalTotals(departments);

  // What waits on other pages (watch suggestions, …): one Needs you row each.
  const elsewhere = today?.waiting_elsewhere ?? [];

  const isQuiet =
    today !== null &&
    elsewhere.length === 0 &&
    today.proposals.length === 0 &&
    replies.cards.length === 0 &&
    actionCards.cards.length === 0 &&
    inviteCards.cards.length === 0 &&
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
  const handledOvernight = today?.handled_overnight ?? [];
  const handledRows = groupHandled(handledOvernight);
  const inFlight = today?.in_flight ?? [];
  const practiceClients = today?.practice_clients ?? [];

  // A team's Departments tile counts its departments at risk.
  const deptAtRiskCount = solo
    ? 0
    : attentionRealDepts.filter((d) => d.at_risk_count > 0 || d.off_track_count > 0).length;
  // Waiting on the owner with no card (Right now's rows): listed in Needs you.
  const rightNowRows = today?.right_now ?? [];
  // What the Executive did or is doing for the viewer today.
  const doneRows = today?.done_for_you ?? [];
  // While a row is Working on it, read again every few seconds so it turns
  // to Done in place; the work goes on whether or not the page is open.
  const working = anyWorking(doneRows);
  useEffect(() => {
    if (!working) return;
    const started = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - started > DONE_POLL_MAX_MS) clearInterval(timer);
      else if (document.visibilityState === "visible") refreshToday();
    }, DONE_POLL_MS);
    return () => clearInterval(timer);
  }, [working, refreshToday]);
  const needsYouCount =
    rightNowRows.length + mineProposals.length + replies.cards.length + inviteCards.cards.length +
    actionCards.cards.length + elsewhere.length;
  // Home's summary says only what its two lists hold; the rest (people,
  // departments, under way, monitoring) is one quiet line at the bottom.
  const summary: SummaryPart[] = today ? homeSummary(needsYouCount, doneRows.length) : [];

  // At a glance — one quiet line, only the items with something to show,
  // what's yours first. Each item reads on its own: the line has no headings.
  const tiles: Tile[] = [];
  if (today) {
    if (glance.actAsMe)
      tiles.push({
        key: "actAsMe",
        href: "/settings/act-as-me",
        text: `${glance.actAsMe} handled as you this week`,
      });
    const view = openLoops.view;
    if (loopOwnerId != null && (openLoops.error || (view && (view.items.length > 0 || view.later > 0)))) {
      const overdue = view?.items.filter((i) => i.overdue).length ?? 0;
      tiles.push({
        key: "dueSoon",
        href: "/tasks",
        text: view ? tasksText(view.items.length, overdue, view.later) : "Tasks",
      });
    }
    if (goals.goals > 0)
      tiles.push({ key: "goals", href: "/goals", text: goalsText(goals.goals, goals.atRisk, goals.unchecked) });
    if (glance.workflows && glance.workflows.scheduled > 0)
      tiles.push({
        key: "workflows",
        href: "/jobs",
        text: `${counted(glance.workflows.scheduled, "workflow", "workflows")} on a schedule`,
      });
    // Watching: what's on the watchlist, with the signals waiting to be read.
    const signals = monitoringProposals.length;
    if ((glance.watching && glance.watching.watched > 0) || signals > 0)
      tiles.push({
        key: "watching",
        ...(signals > 0 ? { panel: "monitoring" as const } : { href: "/watchlist" }),
        text: watchingText(glance.watching?.watched ?? 0, signals),
      });
    if (handledRows.length > 0)
      tiles.push({
        key: "handled",
        panel: "handled",
        text: `${handledRows.length} handled since your last brief`,
      });
    if (solo) {
      if (projects.error || (projects.projects && projects.projects.length > 0))
        tiles.push({
          key: "projects",
          panel: "projects",
          text: projects.projects
            ? counted(projects.projects.length, "active project", "active projects")
            : "Your projects",
        });
      if (weeklyReview)
        tiles.push({ key: "weeklyReview", panel: "weeklyReview", text: "This week's review is ready" });
    } else {
      // Only once departments are set up: the goals with no department are on
      // the Goals item.
      if (activeRealDepts.length > 0)
        tiles.push({
          key: "departments",
          panel: "departments",
          text: departmentsText(
            activeRealDepts.length,
            attentionRealDepts.length,
            deptAtRiskCount > 0,
            activeRealDepts.filter((d) => (d.unchecked_count ?? 0) > 0).length,
          ),
        });
      if (showPeople && today) {
        const people = today.people;
        const busy = people.filter((p) => p.status === "needs_reply" || p.status === "awaiting").length;
        tiles.push({ key: "people", panel: "people", text: peopleText(people.length, busy) });
      }
    }
    if (otherProposals.length > 0)
      tiles.push({ key: "team", panel: "team", text: `${otherProposals.length} waiting on teammates` });
    if (inFlight.length > 0)
      tiles.push({ key: "inFlight", panel: "inFlight", text: `${inFlight.length} under way` });
    if (practiceClients.length > 0)
      tiles.push({
        key: "clients",
        panel: "clients",
        text: counted(practiceClients.length, "parked client", "parked clients"),
      });
  }

  // The Needs you queue: replies waiting first (they came in first, as their
  // own section did), then the proposals in score order, with the approvals
  // one request held together shown as one card. Each item knows how old it
  // is and how Clear all closes it: a card untouched for a few days folds
  // into the "Older (N)" line (lib/needsYou.ts).
  type QueueItem = {
    key: string;
    // Age anchor (ISO): when it came, or last changed.
    at: string;
    // Never folds: a card answered only on itself, or one being sent.
    keep?: boolean;
    // Someone else is waiting on it: it goes first.
    waited?: boolean;
    // Closes it the way its own Skip would, and drops it from the page.
    clear: () => Promise<void>;
    render: (emphasized: boolean) => React.ReactNode;
  };
  const hideAlerts = (ids: number[]) =>
    setActedAlertIds((prev) => {
      const next = new Set(prev);
      ids.forEach((id) => next.add(id));
      return next;
    });
  const clearProposal = async (p: ProposalItem) => {
    // Same reject as Skip, but one already answered elsewhere (404/409)
    // counts as cleared.
    if (p.decision_instance_id != null) await dismissActionCard(p.decision_instance_id);
    else await ackAlert(p.alert_id, "dismissed");
    hideAlerts([p.alert_id]);
  };
  const queue: QueueItem[] = waitedOnFirst<QueueItem>([
    // What's waiting that has no card (mail it couldn't draft, unsent
    // drafts, chat asks): one row each, first, since it came in first.
    ...rightNowRows.map((row) => ({
      key: `rn-${row.key}`,
      at: row.at,
      clear: async () => onDismissRow(row),
      render: () => <RightNowItem row={row} actions={rightNowActions} />,
    })),
    ...replies.cards.map((card) => ({
      key: `reply-${card.decision_id}`,
      at: card.created_at,
      keep: card.status === "executing",
      clear: async () => {
        await dismissReplyCard(card.decision_id);
        replies.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <ReplyCardItem
          card={card}
          onGone={replies.gone}
          onRefresh={replies.refresh}
          onChanged={replies.replace}
          emphasized={emphasized}
        />
      ),
    })),
    ...inviteCards.cards.map((card) => ({
      key: `invite-${card.decision_id}`,
      at: card.created_at,
      clear: async () => {
        await dismissInviteCard(card.decision_id);
        inviteCards.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <InviteCardItem card={card} onGone={inviteCards.gone} emphasized={emphasized} />
      ),
    })),
    ...actionCards.cards.map((card) => ({
      key: `actions-${card.decision_id}`,
      at: card.created_at,
      clear: async () => {
        await dismissActionCard(card.decision_id);
        actionCards.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <ActionCardItem card={card} onGone={actionCards.gone} emphasized={emphasized} />
      ),
    })),
    ...groupByRequest(mineProposals).map((group): QueueItem => {
      if (group.length > 1) {
        return {
          key: `request-${group[0].group_key}`,
          at: newestAnchor(group),
          waited: group.some((p) => Boolean(p.waiting_on)),
          clear: async () => {
            const results = await Promise.allSettled(group.map(clearProposal));
            if (results.some((r) => r.status === "rejected")) throw new Error("Some didn't clear");
          },
          render: (emphasized: boolean) => (
            <RequestGroupCard
              proposals={group}
              emphasized={emphasized}
              onResolved={(done) => {
                hideAlerts(done.map((p) => p.alert_id));
                refreshToday();
              }}
            />
          ),
        };
      }
      const p = group[0];
      return {
        key: `alert-${p.alert_id}`,
        at: ageAnchor(p),
        // A roster request is answered on its card, never swept.
        keep: Boolean(p.roster_request),
        waited: Boolean(p.waiting_on),
        clear: () => clearProposal(p),
        render: (emphasized: boolean) => (
          <ProposalCard
            proposal={p}
            people={today?.people ?? []}
            onContinue={onContinue}
            onApprove={handleApprove}
            onDismiss={handleDismiss}
            onSnooze={handleSnooze}
            onRerun={handleRerun}
            onApproveWithEdits={handleApproveWithEdits}
            onAnswer={handleAnswer}
            defaultBodyExpanded={emphasized}
            emphasized={emphasized}
          />
        ),
      };
    }),
    // What waits on another page: a row that opens it. Never folds into
    // Older and isn't cleared from here; it goes when it's answered there.
    ...elsewhere.map((item) => ({
      key: `elsewhere-${item.key}`,
      at: item.at || new Date().toISOString(),
      keep: true,
      clear: async () => {},
      render: () => <WaitingElsewhereRow item={item} />,
    })),
  ]);
  const { current: currentQueue, older: olderQueue } = splitOlder(
    queue,
    (item) => item.at,
    new Date(),
    (item) => Boolean(item.keep),
  );
  const visibleQueue = showAllNeedsYou ? currentQueue : currentQueue.slice(0, NEEDS_YOU_VISIBLE);
  const hiddenCount = currentQueue.length - visibleQueue.length;
  const handleClearOlder = async () => {
    setClearingOlder(true);
    setOlderNotice(null);
    const { failed } = await clearAll(olderQueue);
    setClearingOlder(false);
    const notice = clearNotice(failed.length, olderQueue.length);
    setOlderNotice(notice);
    if (!notice) setShowOlder(false);
    refreshToday();
  };


  // The first week: the first-week card takes the page's top slot, so the
  // day isn't called quiet and the summary line points at it.
  const firstWeekCard = today?.first_week_card ?? null;
  const firstWeek = Boolean(today?.first_week);
  // With nothing waiting, Needs you isn't shown: the summary line says
  // all clear. It stays in the first week, where its empty line points at
  // the mailbox step, and while one of its notices is up.
  const hideEmptyNeedsYou =
    !firstWeek &&
    queue.length === 0 &&
    !snoozed &&
    !rerunNotice &&
    !replies.notice;
  // The first week's empty Needs you points at the own-mailbox step only
  // while that mailbox isn't connected (null until known, so no flash).
  const [ownMailboxConnected, setOwnMailboxConnected] = useState<boolean | null>(null);
  useEffect(() => {
    if (!firstWeek) return;
    const ctrl = new AbortController();
    getDelegation(ctrl.signal)
      .then((d) => setOwnMailboxConnected(d ? d.gmail.status === "connected" : null))
      .catch(() => undefined);
    return () => ctrl.abort();
  }, [firstWeek]);

  // In the first week the first-week card says what to start on, so the day
  // isn't "quiet" even with nothing on the board yet.
  const quietBanner =
    isQuiet && activeDepts.length > 0 && !firstWeekCard ? (
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
    } else if (part.target.kind === "doneForYou") {
      document.getElementById(DONE_FOR_YOU_ID)?.scrollIntoView({ behavior: "smooth", block: "start" });
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
                  ? firstWeekCard
                    ? "Your first week. Start below."
                    : "All clear — nothing needs you right now."
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

          {loading && <PageSpinner />}
          {error && (
            <div className="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-[15px] text-rose-700 dark:text-rose-300">
              {error}
            </div>
          )}

          {firstWeekCard && <FirstWeekCard card={firstWeekCard} />}

          {bannerSlot && <div className="mb-6">{bannerSlot}</div>}

          {today && (
            <>
              {/* Needs you — decisions routed to the caller, and replies
                  waiting to be sent. */}
              {!hideEmptyNeedsYou && (
                <section id={NEEDS_YOU_ID} className="mb-8 scroll-mt-4">
                <div className="mb-3 flex items-center gap-2">
                  <h2 className="text-lg font-semibold text-fg">
                    Needs you
                    {needsYouCount > 0 && <span className="ml-1.5 font-normal text-fg-muted">({needsYouCount})</span>}
                  </h2>
                  {replies.cards.length > 0 && <InfoTip align="left">{REPLIES_WAITING_TIP}</InfoTip>}
                  {inviteCards.cards.length > 0 && <InfoTip align="left">{INVITE_CARDS_TIP}</InfoTip>}
                  {actionCards.cards.length > 0 && <InfoTip align="left">{ACTION_CARDS_TIP}</InfoTip>}
                </div>
                {snoozed && (
                  <p className="mb-3 flex flex-wrap items-center gap-x-3 text-sm text-fg-muted" role="status">
                    Snoozed until {snoozed.short}.
                    <button
                      type="button"
                      onClick={handleUnsnooze}
                      className="font-medium text-accent hover:underline"
                    >
                      Undo
                    </button>
                  </p>
                )}
                {rerunNotice && (
                  <p className="mb-3 text-sm text-fg-muted" role="status">
                    {rerunNotice}
                  </p>
                )}
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
                    {(hiddenCount > 0 || (showAllNeedsYou && currentQueue.length > NEEDS_YOU_VISIBLE)) && (
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
                    {olderQueue.length > 0 && (
                      <div className="rounded-2xl border border-line bg-surface-elevated">
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
                          <div className="min-w-0 flex-1">
                            <p className="text-[15px] font-semibold text-fg">Older ({olderQueue.length})</p>
                            <p className="text-sm text-fg-muted">Untouched for {NEEDS_YOU_OLDER_AFTER_DAYS} days or more</p>
                          </div>
                          <button
                            type="button"
                            onClick={() => setShowOlder((v) => !v)}
                            aria-expanded={showOlder}
                            className={buttonClass("secondary", "sm")}
                          >
                            {showOlder ? "Hide" : "Show"}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleClearOlder()}
                            disabled={clearingOlder}
                            className={buttonClass("ghost", "sm")}
                          >
                            {clearingOlder ? "Clearing…" : "Clear all"}
                          </button>
                        </div>
                        {olderNotice && (
                          <p className="px-4 pb-3 text-sm text-red-500" role="status">
                            {olderNotice}
                          </p>
                        )}
                        {showOlder && (
                          <div className="space-y-4 border-t border-line p-3">
                            {olderQueue.map((item) => (
                              <div key={item.key}>{item.render(false)}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ) : firstWeek ? (
                  <p className="rounded-2xl border border-dashed border-line px-4 py-5 text-[15px] text-fg-muted">
                    Nothing waiting on you yet. Decisions and replies to send land here.
                    {ownMailboxConnected === false && (
                      <>
                        {" "}
                        <Link href={FIRST_RUN_LINKS.ownMailbox} className="font-medium text-accent hover:underline">
                          Let it read your mailbox
                        </Link>{" "}
                        to get draft replies here.
                      </>
                    )}
                  </p>
                ) : null}
                </section>
              )}

              <DoneForYou
                rows={doneRows}
                onRetry={(row) => void handleRetryApproval(row)}
                onOpenChat={onOpenChat}
              />

              <WatchingNext items={today?.watching ?? []} />

              {/* Everything else, one quiet line: each opens its panel or page. */}
              {tiles.length > 0 && (
                <nav aria-label="More" className="mb-8 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-fg-muted">
                  {tiles.map((t) => {
                    const text = t.text;
                    const cls = "hover:text-fg hover:underline underline-offset-4";
                    const panel = t.panel;
                    return panel ? (
                      <button key={t.key} type="button" onClick={() => setOpenPanel(panel)} aria-haspopup="dialog" className={cls}>
                        {text}
                      </button>
                    ) : (
                      <Link key={t.key} href={t.href ?? "/"} className={cls}>
                        {text}
                      </Link>
                    );
                  })}
                </nav>
              )}

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
                      // Someone else's card is theirs (or the principal's)
                      // to approve; the server refuses anyone else.
                      onApprove={isPrincipalCaller ? handleApprove : undefined}
                      onDismiss={handleDismiss}
                      onSnooze={handleSnooze}
                      onRerun={handleRerun}
                      onApproveWithEdits={isPrincipalCaller ? handleApproveWithEdits : undefined}
                      onAnswer={isPrincipalCaller ? handleAnswer : undefined}
                    />
                  ))}
                  {otherProposals.length === 0 && (
                    <p className="text-[15px] text-fg-muted">
                      Nothing waiting on anyone else.
                      {today.people.length <= 1 && " Add your team on People to see their work here."}
                    </p>
                  )}
                </div>
              </SidePanel>
              <SidePanel open={openPanel === "inFlight"} onClose={() => setOpenPanel(null)} title="Under way">
                <InFlightPanelBody inFlight={inFlight} />
              </SidePanel>
              <SidePanel open={openPanel === "handled"} onClose={() => setOpenPanel(null)} title="Handled">
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
