"use client";

import Link from "next/link";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import RosterRequestCard from "@/components/RosterRequestCard";
import Button, { buttonClass } from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import { type PersonBriefItem, type PlanResult, type ProposalItem, type SteerResult, undoCardPlan } from "@/lib/api";
import { briefingMemoryLine, nudgeAction } from "@/lib/briefing-memory";
import { ageLabel, daysUntil, proposalStatusChip } from "@/lib/briefingSummary";
import { cardButtons, isSignalCard } from "@/lib/needsYou";
import { type SnoozeChoice, snoozeChoices } from "@/lib/snooze";
import { hostOf } from "@/lib/url";

import CardAsk from "./CardAsk";
import CardPlanReply from "./CardPlanReply";
import CardSteer from "./CardSteer";
import CardTopRow from "./CardTopRow";
import HeldActionCard, { isHeldAction } from "./HeldActionCard";
import LeadTrainingCard, { isLeadCard } from "./LeadTrainingCard";
import { Chip, TONE_TEXT, type ContinueHandler } from "./shared";

// A proposal body longer than about half this (chars) is clamped to two
// lines under the headline while the card is closed, so the "Needs you"
// queue stays scannable.
const LONG_BODY_CHARS = 180;

// Badge text for an artifact card: the format when it isn't plain Markdown.
function artifactBadge(format: ProposalItem["artifact_format"]): string {
  switch (format) {
    case "html":
      return "Web page";
    case "docx":
      return "Word doc";
    case "xlsx":
      return "Spreadsheet";
    case "link":
      return "Link";
    default:
      return "Document";
  }
}

// Lifecycle presentation for one proposal card — the review-driven pieces
// the opened card shows: the one-line "what changed since you last looked",
// the chip row (age, seen ×N, why-now / due, likely-stale, folded-in,
// draft-ready) and the muted "Reviewed <ago>" footer. A pure builder (no
// hooks) so ProposalCard stays a layout function.
function buildProposalLifecycle(
  proposal: ProposalItem,
  ageShown: boolean,
): {
  reviewLine: React.ReactNode;
  lifecycleRow: React.ReactNode;
  reviewedFooter: React.ReactNode;
} {
  const verdict = proposal.review_verdict ?? "";
  const isLikelyStale = verdict === "likely_stale";
  const age = ageLabel(proposal.created_at);
  const dueIn = daysUntil(proposal.due_at);
  const reviewLine = proposal.review_note ? (
    <p className={`text-sm leading-snug ${isLikelyStale ? TONE_TEXT.amber : "text-fg-muted"}`}>
      <span className="mr-1.5 text-xs font-semibold uppercase tracking-wide">
        {isLikelyStale ? "Likely stale" : verdict === "changed" ? "Updated" : "Since you last looked"}
      </span>
      {proposal.review_note}
    </p>
  ) : null;
  const chips: { label: string; tone: "neutral" | "amber" | "sky"; title?: string }[] = [];
  // The status chip already reads "2m old" when nothing is due.
  if (age && !ageShown) chips.push({ label: age, tone: "neutral", title: `Raised ${new Date(proposal.created_at).toLocaleString()}` });
  if ((proposal.occurrence_count ?? 1) > 1)
    chips.push({
      label: `seen ×${proposal.occurrence_count}`,
      tone: "neutral",
      title: proposal.last_seen_at ? `Last seen ${ageLabel(proposal.last_seen_at)} ago` : undefined,
    });
  if (proposal.why_now || dueIn != null) {
    const due = dueIn == null ? "" : dueIn < 0 ? "overdue" : dueIn === 0 ? "due today" : `due in ${dueIn}d`;
    chips.push({ label: [proposal.why_now, due].filter(Boolean).join(" · "), tone: "amber" });
  }
  if (isLikelyStale && !proposal.review_note) chips.push({ label: "Likely stale", tone: "amber" });
  if ((proposal.superseded_count ?? 0) > 0)
    chips.push({ label: `${proposal.superseded_count} folded in`, tone: "neutral" });
  if (verdict === "changed" && !proposal.review_note)
    chips.push({ label: "Updated by the Executive", tone: "sky" });
  if (verdict === "drafted") chips.push({ label: "Draft ready in your queue", tone: "amber" });
  const lifecycleRow = chips.length > 0 ? (
    <div className="flex flex-wrap gap-1.5">
      {chips.map((c) => (
        <Chip key={c.label} tone={c.tone} title={c.title}>
          {c.label}
        </Chip>
      ))}
    </div>
  ) : null;
  const reviewedFooter = proposal.last_reviewed_at ? (
    <p className="text-xs text-fg-subtle">
      Reviewed {ageLabel(proposal.last_reviewed_at)} ago
      {verdict && verdict !== "likely_stale" ? ` · ${verdict}` : ""}
    </p>
  ) : null;
  return { reviewLine, lifecycleRow, reviewedFooter };
}

// One action a card can offer; the card shows one as its primary button and
// the rest in its ⋯ menu.
interface CardAction {
  label: string;
  onSelect?: () => void;
  href?: string;
  external?: boolean;
  danger?: boolean;
}

// What the card needs from its reader (ProposalItem.needs), as the chip at
// its top says it.
const NEEDS_CHIP: Record<string, { label: string; tone: "sky" | "amber" | "emerald" | "neutral" }> = {
  answer: { label: "Needs your answer", tone: "sky" },
  choice: { label: "Pick one", tone: "amber" },
  ok: { label: "Needs your OK", tone: "emerald" },
  fyi: { label: "For you to know", tone: "neutral" },
};

// A card in "Needs you" (and in the Across-the-team panel). Closed, it shows
// the proposal, one status chip and who it is routed to, with one primary
// button (the recommended move, or Approve) and a reply box that changes
// what Approve does (CardPlanReply); the rest sit in
// ⋯. A click on the card opens it to show the review line, the lifecycle
// chips, what approving does, the tags and when it was last reviewed.
export default function ProposalCard({
  proposal,
  people,
  onContinue,
  onApprove,
  onDismiss,
  onSnooze,
  onRerun,
  onApproveWithEdits,
  onAnswer,
  onSteered,
  onSteerUndone,
  busy = false,
  defaultBodyExpanded = false,
  emphasized = false,
}: {
  proposal: ProposalItem;
  people: PersonBriefItem[];
  onContinue?: ContinueHandler;
  onApprove?: (p: ProposalItem) => void;
  onDismiss?: (p: ProposalItem) => void;
  // Hide the card until a later time; it comes back on its own.
  onSnooze?: (p: ProposalItem, choice: SnoozeChoice) => void;
  // Queue a failed scheduled run again (cards with `failed_run_id`).
  onRerun?: (p: ProposalItem) => void;
  onApproveWithEdits?: (p: ProposalItem, editedBody: string) => void;
  // Answer a card that asks something (needs "answer" or
  // "choice"); rejects with the reason when it couldn't be sent.
  onAnswer?: (p: ProposalItem, answer: string) => Promise<void>;
  // A watch's card (watch_slug): the person replied to steer the watch.
  // The card stays to show what changed; the page drops the others it closed.
  onSteered?: (p: ProposalItem, result: SteerResult) => void;
  onSteerUndone?: (p: ProposalItem, result: SteerResult) => void;
  busy?: boolean;
  // Show the whole body while closed (no clamp) — the first card in the
  // queue, so its full text reads without a click.
  defaultBodyExpanded?: boolean;
  // The first card in the queue: an accent border so the single sharpest
  // item stands out.
  emphasized?: boolean;
}) {
  // Local edit-mode state. Entering edit mode replaces the body with a
  // textarea pre-filled with the proposal body; the action row becomes
  // Cancel / Send approval. Send approval pipes the edited text up through
  // onApproveWithEdits, and the server sends exactly that text
  // (POST /alerts/{id}/approve).
  const [editing, setEditing] = useState(false);
  const [editedBody, setEditedBody] = useState("");
  const [expanded, setExpanded] = useState(false);
  // A reply on a watch's card closed it: its buttons go until Undo.
  const [steered, setSteered] = useState(false);
  // The plan after a reply on the card changed it (or Undo change put one
  // back), over the card as it was loaded (`from`): once the page reloads
  // the card with another plan, the card's own wins.
  const [planNow, setPlanNow] = useState<{ plan: string; changed: boolean; from: string } | null>(null);
  const [undoing, setUndoing] = useState(false);
  const [undoError, setUndoError] = useState<string | null>(null);
  // Bumped by Undo change, so the reply box drops what it last said.
  const [undos, setUndos] = useState(0);
  // A roster request ("who is this new sender?") is answered on its own
  // card — never through chat, so what a stranger wrote never seeds a turn.
  // Below the hooks, which must run on every render.
  if (proposal.roster_request) {
    return (
      <RosterRequestCard
        request={proposal.roster_request}
        at={proposal.created_at}
        emphasized={emphasized}
        onResolved={() => onDismiss?.(proposal)}
      />
    );
  }
  // A Take the lead card is answered on its own card too: Approve (with the
  // "on its own next time" tick in training), Edit and Decline (LeadTrainingCard).
  if (isLeadCard(proposal)) {
    return <LeadTrainingCard proposal={proposal} emphasized={emphasized} onResolved={() => onDismiss?.(proposal)} />;
  }
  // A team member's chat send their department held: approved on its own
  // card once the whole call is on screen (HeldActionCard).
  if (isHeldAction(proposal)) {
    return <HeldActionCard proposal={proposal} emphasized={emphasized} onResolved={() => onDismiss?.(proposal)} />;
  }
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
  const assignee =
    proposal.routed_to_person_id != null
      ? people.find((p) => p.id === proposal.routed_to_person_id)
      : null;
  // Research artifacts (the Executive's `draft_artifact` tool) are full
  // authored documents, not terse proposals; the `artifact` topic tag is the
  // discriminator. They are read and marked reviewed, not approved.
  const isArtifact = proposal.topic_tags?.includes("artifact") ?? false;
  // Monitoring items are passive external/watchlist signals — nothing to
  // approve, so the body reads as "why it's on your radar".
  const isMonitoring = proposal.category === "monitoring";
  // A signal from outside (a watched stock's move, an app's new entries) is
  // for you to know even when its severity lifted it into the action lane:
  // nothing to approve, Got it clears it.
  const isSignal = isSignalCard(proposal);
  // Decision-backed proposals (gated calendar bookings) execute server-side
  // via the /decisions endpoints. The verbatim-DM "Edit & approve" flow
  // doesn't map to a calendar booking, so it's not offered for these.
  const isDecision = proposal.decision_instance_id != null;
  const isLikelyStale = (proposal.review_verdict ?? "") === "likely_stale";
  // The card's title is the headline; under it, the rest of the body — what
  // follows the headline when the body opens with it, else the whole body.
  // Monitoring signals show their body as "why it's on your radar" when
  // opened, and artifacts show the document.
  const description = (() => {
    // A document's card says why it was made, without opening Details.
    if (isArtifact) return (proposal.suggested_action || "").trim();
    if (isMonitoring) return "";
    const body = proposal.body.trim();
    const headline = proposal.headline.trim();
    if (!body || body === headline) return "";
    if (headline && body.startsWith(headline)) {
      const rest = body.slice(headline.length).replace(/^[\s.:;,—–-]+/, "");
      return rest.charAt(0).toUpperCase() + rest.slice(1);
    }
    return body;
  })();
  const isLongBody = description.length > LONG_BODY_CHARS / 2;
  const clamp = isLongBody && !expanded && !defaultBodyExpanded;
  // What approving commits the Executive to ("if I approve, the exec will
  // do THIS"). Never for monitoring; a document shows its why under the title.
  const loadedPlan = proposal.suggested_action ?? "";
  const local = planNow && planNow.from === loadedPlan ? planNow : null;
  const plan = local?.plan ?? loadedPlan;
  const planChanged = local?.changed ?? Boolean(proposal.plan_changed);
  const suggestedActionBlock =
    !isMonitoring && !isArtifact && plan ? (
      <div
        className={`rounded-xl border px-3.5 py-2.5 ${
          planChanged ? "border-accent/70 bg-accent/10" : "border-accent/30 bg-accent/5"
        }`}
      >
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-accent">
          {planChanged
            ? local
              ? "If you approve · Updated"
              : `If you approve · Updated${proposal.plan_changed_by ? ` by ${proposal.plan_changed_by}` : ""}`
            : "If you approve"}
        </div>
        <p className="text-sm leading-snug text-fg whitespace-pre-wrap break-words">{plan}</p>
      </div>
    ) : null;

  if (editing) {
    return (
      <div
        id={`alert-${proposal.alert_id}`}
        className="rounded-2xl border border-accent/50 bg-surface-elevated p-4 sm:p-5"
      >
        <div className="mb-3 flex items-start justify-between gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
            Editing proposal — sent word for word
          </span>
          {assignee && <span className="flex-shrink-0 text-sm text-accent">→ {assignee.full_name}</span>}
        </div>
        {/* What the exec will do on approval, so the user sees what they're
            authorizing while editing the message body. */}
        {suggestedActionBlock && <div className="mb-3">{suggestedActionBlock}</div>}
        <textarea
          value={editedBody}
          onChange={(e) => setEditedBody(e.target.value)}
          rows={6}
          disabled={busy}
          aria-label="Proposal text to send"
          className="w-full rounded-xl border border-line bg-surface p-3 text-[15px] leading-snug text-fg whitespace-pre-wrap focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50"
          autoFocus
        />
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          <Button variant="ghost" onClick={cancelEditing} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submitEdit} disabled={busy || !editedBody.trim()}>
            Send approval
          </Button>
        </div>
      </div>
    );
  }

  // The actions this card offers, then the pick of one as the primary.
  const firstName = assignee?.full_name?.split(" ")[0];
  const move = proposal.recommended_move ?? "";
  const nudge: CardAction | null =
    move === "nudge" && onContinue && !isArtifact && !isMonitoring
      ? {
          label: `Nudge ${firstName ?? "owner"}`,
          onSelect: () => {
            const who = assignee?.full_name ?? "the owner";
            onContinue(
              `Nudge ${who} about this item — it has gone quiet: ${proposal.headline}\n\n` +
                `Send a short, friendly check-in via message_person and tell me what you sent.`,
              briefingMemoryLine(nudgeAction(who), proposal.headline),
            );
          },
        }
      : null;
  const workflow: CardAction | null =
    move === "suggest_workflow" && proposal.suggested_workflow && !isArtifact && !isMonitoring
      ? {
          label: `Run ${proposal.suggested_workflow.replace(/_/g, " ")}`,
          href: `/jobs/${proposal.suggested_workflow}`,
        }
      : null;
  // A scheduled run that failed: Run again leads, the run page is a click
  // away, and there is nothing to approve.
  const failedRun = proposal.failed_run_id ?? null;
  const rerun: CardAction | null =
    failedRun && onRerun ? { label: "Run again", onSelect: () => onRerun(proposal) } : null;
  const openRun: CardAction | null = failedRun
    ? { label: "Open the run", href: `/jobs/runs/${encodeURIComponent(failedRun)}` }
    : null;
  // A card that asks something is answered on the card, and one that's for
  // them to know has nothing to approve.
  const needs = proposal.needs ?? "";
  const asks = (needs === "answer" || needs === "choice") && Boolean(onAnswer);
  const { noApprove, gotIt } = cardButtons(needs, isSignal);
  const approve: CardAction | null =
    !noApprove && !isMonitoring && !isArtifact && !failedRun && onApprove
      ? {
          label: "Approve",
          // The plan as this card shows it, which the server checks.
          onSelect: () => onApprove({ ...proposal, suggested_action: plan }),
        }
      : null;
  // A reply on the card changes what Approve does (alerts/replan.py): only
  // on a card whose Approve carries its plan out, as the server says.
  const canReplan = Boolean(approve) && Boolean(proposal.plan_editable) && !isDecision;
  async function undoPlan() {
    if (undoing) return;
    setUndoing(true);
    setUndoError(null);
    try {
      const r = await undoCardPlan(proposal.alert_id);
      setPlanNow({ plan: r.plan, changed: r.changed, from: loadedPlan });
      setUndos((n) => n + 1);
    } catch (e) {
      setUndoError(e instanceof Error ? e.message : "Couldn't undo that. Try again.");
    } finally {
      setUndoing(false);
    }
  }
  const editApprove: CardAction | null =
    !noApprove && !isMonitoring && !isArtifact && !isDecision && !failedRun && onApproveWithEdits
      ? { label: "Edit & approve", onSelect: startEditing }
      : null;
  // Dismiss sits apart at the right of every card; a card that is only for
  // you to know leads with it as Got it instead.
  const dismiss: CardAction | null = onDismiss
    ? { label: gotIt ? "Got it" : "Dismiss", onSelect: () => onDismiss(proposal) }
    : null;
  const openDoc: CardAction | null = isArtifact
    ? { label: "Open document", href: `/artifacts/${encodeURIComponent(`alert:${proposal.alert_id}`)}` }
    : null;
  const openInApp: CardAction | null =
    isArtifact && proposal.artifact_format === "link" && proposal.artifact_url
      ? { label: `Open in app (${hostOf(proposal.artifact_url)})`, href: proposal.artifact_url, external: true }
      : null;
  const personPage: CardAction | null = assignee
    ? { label: `Open ${firstName ?? assignee.full_name}'s page`, href: `/people/${assignee.id}` }
    : null;

  // The primary: the review's recommended move when it is one the user
  // completes (nudge, run a workflow); Dismiss when the review thinks the
  // card is stale; Open for a document; otherwise Approve.
  let primary: CardAction | null;
  if (isArtifact) primary = openDoc;
  else if (asks) primary = null; // answered on the card
  else if (gotIt) primary = dismiss; // Got it
  else if (isMonitoring) primary = null; // nothing to approve
  else if (isLikelyStale && dismiss) primary = dismiss;
  else primary = rerun ?? nudge ?? workflow ?? approve;
  // Snooze: one item per choice, just above Dismiss. Not offered on a
  // drafted document, which lives in the gallery rather than the queue.
  const snoozes: CardAction[] =
    onSnooze && !isArtifact
      ? snoozeChoices().map((c) => ({ label: c.label, onSelect: () => onSnooze(proposal, c) }))
      : [];
  const menu = [rerun, openRun, nudge, workflow, approve, editApprove, ...snoozes, openInApp, personPage].filter(
    (a): a is CardAction => a != null && a !== primary,
  );
  const sideDismiss = dismiss && dismiss !== primary ? dismiss : null;
  const showActions = Boolean(onApprove || onDismiss || onApproveWithEdits);

  const status = isMonitoring ? null : proposalStatusChip(proposal);
  // Tags like "private:…", "decision_instance:12" or "meetings:…" only route
  // or link the card; they mean nothing to the reader. Outside-world tags stay.
  const visibleTags = proposal.topic_tags.filter(
    (t) => t !== "artifact" && (!t.includes(":") || t.startsWith("external:")),
  );
  // The top row always shows its age.
  const { reviewLine, lifecycleRow, reviewedFooter } = buildProposalLifecycle(proposal, true);
  const surfacedNote = proposal.surfaced_reason ? (
    <p className="text-sm text-fg-muted leading-snug">{proposal.surfaced_reason}</p>
  ) : null;
  const radarBody =
    isMonitoring && proposal.body && proposal.body !== proposal.headline ? proposal.body : null;
  const hasDetails = Boolean(
    reviewLine ||
      lifecycleRow ||
      (suggestedActionBlock && !canReplan) ||
      surfacedNote ||
      visibleTags.length > 0 ||
      reviewedFooter ||
      isArtifact ||
      radarBody ||
      isLongBody,
  );

  // The top row: what it needs from you, then As me or where it came from,
  // then its age; a due date or a likely-stale review joins it.
  const need = isArtifact
    ? artifactBadge(proposal.artifact_format)
    : NEEDS_CHIP[needs]?.label ??
      (failedRun ? "Didn't finish" : gotIt ? "For you to know" : approve ? "Needs your OK" : undefined);
  const source = failedRun ? "Workflow" : isSignal ? "Watch list" : undefined;
  const flag = status && !status.label.endsWith(" old") ? status : null;
  const header = (
    <>
      <CardTopRow
        need={need}
        mode={source ? undefined : "take_the_lead"}
        source={source}
        at={proposal.created_at}
        waitingOn={proposal.waiting_on}
      />
      <div
        className={`text-base sm:text-[17px] font-semibold leading-snug text-fg whitespace-pre-wrap break-words${
          expanded ? "" : " line-clamp-3"
        }`}
      >
        {proposal.headline}
      </div>
      {description && (
        <p
          className={`mt-1.5 text-[15px] leading-relaxed text-fg-muted whitespace-pre-wrap break-words${
            clamp ? " line-clamp-2" : ""
          }`}
          title={clamp ? description : undefined}
        >
          {description}
        </p>
      )}
      {(flag || hasDetails) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm text-fg-muted">
          {flag && <Chip tone={flag.tone}>{flag.label}</Chip>}
          {hasDetails && (
            <span className="inline-flex items-center gap-1 text-fg-subtle">
              {expanded ? "Less" : "Details"}
              <span aria-hidden="true" className={`text-[10px] transition-transform ${expanded ? "rotate-90" : ""}`}>
                ▸
              </span>
            </span>
          )}
        </div>
      )}
    </>
  );

  return (
    <div
      id={`alert-${proposal.alert_id}`}
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 transition-colors ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      {hasDetails ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="block w-full cursor-pointer rounded-lg text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          {header}
        </button>
      ) : (
        <div>{header}</div>
      )}

      {approve && proposal.waiting_because && (
        <p className="mt-2 text-sm leading-snug text-fg-muted break-words">
          <span className="font-semibold text-fg">Waiting because:</span> {proposal.waiting_because}
        </p>
      )}

      {canReplan && (
        <>
          {suggestedActionBlock && <div className="mt-3">{suggestedActionBlock}</div>}
          <CardPlanReply
            key={undos}
            alertId={proposal.alert_id}
            changed={planChanged}
            disabled={busy || undoing}
            onChanged={(r: PlanResult) => {
              setUndoError(null);
              setPlanNow({ plan: r.plan, changed: r.changed, from: loadedPlan });
            }}
          />
        </>
      )}

      {expanded && (
        <div className="mt-3 space-y-2.5">
          {radarBody && (
            <div>
              <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-sky-700 dark:text-sky-300">
                Why this is on your radar
              </div>
              <p className="text-sm text-fg-muted whitespace-pre-wrap break-words">{radarBody}</p>
            </div>
          )}
          {reviewLine}
          {lifecycleRow}
          {!canReplan && suggestedActionBlock}
          {surfacedNote}
          {visibleTags.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {visibleTags.map((t) => (
                // `external:*` tags come from the external-monitoring layer —
                // sky marks outside-world provenance at a glance.
                <Chip key={t} tone={t.startsWith("external:") ? "sky" : "neutral"}>
                  {t}
                </Chip>
              ))}
            </div>
          )}
          {isArtifact && (
            // Full authored document, rendered as Markdown in a bounded,
            // scrollable region so a long brief can't blow out the card.
            <div className="max-h-80 overflow-y-auto rounded-xl border border-line bg-surface/40 px-3.5 py-2.5 prose prose-invert prose-sm max-w-none text-sm text-fg-muted leading-relaxed [&_*]:break-words">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{proposal.body || proposal.headline}</ReactMarkdown>
            </div>
          )}
          {reviewedFooter}
        </div>
      )}

      {asks && onAnswer && (
        <CardAsk
          alertId={proposal.alert_id}
          question={proposal.question ?? ""}
          choices={needs === "choice" ? (proposal.choices ?? []) : []}
          onAnswer={(answer) => onAnswer(proposal, answer)}
        />
      )}

      {proposal.watch_slug && onDismiss && !asks && (
        <CardSteer
          alertId={proposal.alert_id}
          watchSlug={proposal.watch_slug}
          onSteered={(r) => {
            setSteered(true);
            onSteered?.(proposal, r);
          }}
          onUndone={(r) => {
            setSteered(false);
            onSteerUndone?.(proposal, r);
          }}
        />
      )}

      {showActions && !steered && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {primary &&
            (primary.href ? (
              <Link href={primary.href} className={buttonClass("primary", "md")}>
                {primary.label}
              </Link>
            ) : (
              <Button variant="primary" onClick={primary.onSelect} disabled={busy}>
                {primary.label}
              </Button>
            ))}
          {canReplan && planChanged && (
            <Button variant="ghost" onClick={() => void undoPlan()} disabled={busy || undoing}>
              {undoing ? "Undoing…" : "Undo change"}
            </Button>
          )}
          {menu.length > 0 && (
            <OverflowMenu
              label="More actions for this item"
              items={menu.map(
                (a): OverflowItem => ({
                  label: a.label,
                  onSelect: a.onSelect,
                  href: a.href,
                  external: a.external,
                  danger: a.danger,
                  disabled: busy && !a.href,
                }),
              )}
            />
          )}
          {sideDismiss && (
            // Dismiss is its own button, apart on the right, in the same
            // place on every card.
            <Button variant="ghost" className="ml-auto" onClick={sideDismiss.onSelect} disabled={busy}>
              {sideDismiss.label}
            </Button>
          )}
        </div>
      )}
      {undoError && (
        <p role="alert" className="mt-1 text-sm text-red-500">
          {undoError}
        </p>
      )}
    </div>
  );
}
