"use client";

import Link from "next/link";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import RosterRequestCard from "@/components/RosterRequestCard";
import Button, { buttonClass } from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import type { PersonBriefItem, ProposalItem } from "@/lib/api";
import { MEMORY_ACTIONS, briefingMemoryLine, nudgeAction } from "@/lib/briefing-memory";
import { ageLabel, daysUntil, proposalStatusChip } from "@/lib/briefingSummary";
import { hostOf } from "@/lib/url";

import LeadTrainingCard, { isLeadTraining } from "./LeadTrainingCard";
import { Chip, TONE_TEXT, buildMonitoringSeed, type ContinueHandler } from "./shared";

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
function buildProposalLifecycle(proposal: ProposalItem): {
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
  if (age) chips.push({ label: age, tone: "neutral", title: `Raised ${new Date(proposal.created_at).toLocaleString()}` });
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

// The chat seed for Discuss: the FULL body plus the suggested_action, so the
// Executive has the entire card's worth of context, and a mode primer (what
// it may and may not do from chat) that depends on the kind of card.
function buildHandoffPrompt(
  proposal: ProposalItem,
  kind: { isArtifact: boolean; isMonitoring: boolean; isDecision: boolean },
): string {
  if (kind.isArtifact) {
    // Artifacts aren't approved/executed — they're read. Seed the chat
    // with the full document + rationale so the Executive can discuss
    // it, and let it clear the card via ack_alert when the user is done.
    const rationale = proposal.suggested_action
      ? `\n\nWhy you flagged it: ${proposal.suggested_action}`
      : "";
    return (
      `Let's discuss this artifact you flagged for my review:\n\n` +
      `# ${proposal.headline}\n\n${proposal.body || ""}${rationale}\n\n` +
      `[Discuss mode — alert_id=${proposal.alert_id}, artifact id ` +
      `alert:${proposal.alert_id}; to revise it, publish a new version with ` +
      `draft_artifact(supersedes="alert:${proposal.alert_id}")] This is a document ` +
      `for review, not an action to approve. Answer my questions about it ` +
      `conversationally. When I say I'm done ("got it", "reviewed", "thanks"), ` +
      `call ack_alert(alert_id=${proposal.alert_id}, status="ack") to clear ` +
      `it from my queue. Take no other action.`
    );
  }
  if (kind.isMonitoring) {
    // Monitoring signals are passive — there's nothing to approve, so the
    // Discuss handoff asks the Executive to interpret the signal.
    return buildMonitoringSeed(proposal);
  }
  if (kind.isDecision) {
    // Gated calendar booking. Approval/rejection happens via the card's
    // Approve/Dismiss (the /decisions endpoints book or cancel the event
    // server-side) — NOT via chat. So the Discuss handoff is read-only.
    const body = proposal.body || proposal.headline;
    return (
      `Let's talk through this meeting I've proposed:\n\n${body}\n\n` +
      `[Discuss mode] This booking is awaiting your approval on the ` +
      `briefing. Help me decide whether the time, attendees, and purpose ` +
      `make sense. Do NOT book, cancel, or ack anything from chat — I'll ` +
      `approve or dismiss it from the card itself.`
    );
  }
  // alert_id is carried so the exec can clear the card once approval lands.
  const text = proposal.body || proposal.headline;
  const suggested = proposal.suggested_action
    ? `\n\nIf I approve, you will:\n${proposal.suggested_action}`
    : "";
  const primer =
    `\n\n[Discuss mode — alert_id=${proposal.alert_id}] ` +
    `This proposal is NOT YET approved. Answer my questions conversationally. ` +
    `When I explicitly approve ("ok", "approve", "go ahead", "do it"), switch to ` +
    `execute mode: actually attempt the work (use web_search and your other tools — ` +
    `don't just promise), reply inline with the deliverable, schedule a fresh ` +
    `follow-up via schedule_followup if it's time-bound, and call ` +
    `ack_alert(alert_id=${proposal.alert_id}, status="ack"). ` +
    `If I dismiss it ("never mind", "drop it"), call ` +
    `ack_alert(alert_id=${proposal.alert_id}, status="dismissed") and stop. ` +
    `Until explicit approval/dismissal, take no action and do not ack.`;
  return `Tell me about this proposal:\n\n${text}${suggested}${primer}`;
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

// A card in "Needs you" (and in the Across-the-team panel). Closed, it shows
// the proposal, one status chip and who it is routed to, with one primary
// button (the recommended move, or Approve) beside Discuss; the rest sit in
// ⋯. A click on the card opens it to show the review line, the lifecycle
// chips, what approving does, the tags and when it was last reviewed.
export default function ProposalCard({
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
  onContinue?: ContinueHandler;
  onApprove?: (p: ProposalItem) => void;
  onDismiss?: (p: ProposalItem) => void;
  onApproveWithEdits?: (p: ProposalItem, editedBody: string) => void;
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
  // onApproveWithEdits so Briefing can ack the alert + seed the chat with a
  // verbatim-send instruction.
  const [editing, setEditing] = useState(false);
  const [editedBody, setEditedBody] = useState("");
  const [expanded, setExpanded] = useState(false);
  // A roster request ("who is this new sender?") is answered on its own
  // card — never through chat, so what a stranger wrote never seeds a turn.
  // Below the hooks, which must run on every render.
  if (proposal.roster_request) {
    return (
      <RosterRequestCard
        request={proposal.roster_request}
        emphasized={emphasized}
        onResolved={() => onDismiss?.(proposal)}
      />
    );
  }
  // A Take the lead card from training is answered on its own card too:
  // Approve, Approve + allow, Edit and Decline (LeadTrainingCard).
  if (isLeadTraining(proposal)) {
    return <LeadTrainingCard proposal={proposal} emphasized={emphasized} onResolved={() => onDismiss?.(proposal)} />;
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
    if (isMonitoring || isArtifact) return "";
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
  const handoffPrompt = buildHandoffPrompt(proposal, { isArtifact, isMonitoring, isDecision });
  const handoffMemory = briefingMemoryLine(
    isArtifact
      ? MEMORY_ACTIONS.artifact
      : isMonitoring
        ? MEMORY_ACTIONS.monitoring
        : isDecision
          ? MEMORY_ACTIONS.meeting
          : MEMORY_ACTIONS.proposal,
    proposal.headline,
  );
  // What approving commits the Executive to ("if I approve, the exec will
  // do THIS"); for an artifact, why it's worth reading. Never for monitoring.
  const suggestedActionBlock =
    !isMonitoring && proposal.suggested_action ? (
      <div className="rounded-xl border border-accent/30 bg-accent/5 px-3.5 py-2.5">
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-accent">
          {isArtifact ? "Why this is worth your time" : "If you approve"}
        </div>
        <p className="text-sm leading-snug text-fg whitespace-pre-wrap break-words">
          {proposal.suggested_action}
        </p>
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
  const approve: CardAction | null =
    !isMonitoring && !isArtifact && onApprove ? { label: "Approve", onSelect: () => onApprove(proposal) } : null;
  const editApprove: CardAction | null =
    !isMonitoring && !isArtifact && !isDecision && onApproveWithEdits
      ? { label: "Edit & approve", onSelect: startEditing }
      : null;
  const dismiss: CardAction | null = onDismiss
    ? isArtifact
      ? { label: "Mark reviewed", onSelect: () => onDismiss(proposal) }
      : { label: isMonitoring ? "Dismiss signal" : "Dismiss", onSelect: () => onDismiss(proposal) }
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
  else if (isMonitoring) primary = null; // Discuss leads
  else if (isLikelyStale && dismiss) primary = dismiss;
  else primary = nudge ?? workflow ?? approve;
  const menu = [nudge, workflow, approve, editApprove, dismiss, openInApp, personPage].filter(
    (a): a is CardAction => a != null && a !== primary,
  );
  const showActions = Boolean(onApprove || onDismiss || onApproveWithEdits);

  const status = isMonitoring ? null : proposalStatusChip(proposal);
  const visibleTags = proposal.topic_tags.filter((t) => t !== "artifact");
  const { reviewLine, lifecycleRow, reviewedFooter } = buildProposalLifecycle(proposal);
  const surfacedNote = proposal.surfaced_reason ? (
    <p className="text-sm text-fg-muted leading-snug">{proposal.surfaced_reason}</p>
  ) : null;
  const radarBody =
    isMonitoring && proposal.body && proposal.body !== proposal.headline ? proposal.body : null;
  const hasDetails = Boolean(
    reviewLine ||
      lifecycleRow ||
      suggestedActionBlock ||
      surfacedNote ||
      visibleTags.length > 0 ||
      reviewedFooter ||
      isArtifact ||
      radarBody ||
      isLongBody,
  );

  const header = (
    <>
      {isArtifact && (
        <div className="mb-1.5">
          <Chip tone="amber">{artifactBadge(proposal.artifact_format)}</Chip>
        </div>
      )}
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
      {(status || assignee || hasDetails) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm text-fg-muted">
          {status && <Chip tone={status.tone}>{status.label}</Chip>}
          {assignee && <span>→ {assignee.full_name}</span>}
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
          {suggestedActionBlock}
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

      {showActions && (
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
          {onContinue && (
            <Button
              variant={primary ? "secondary" : "primary"}
              onClick={() => onContinue(handoffPrompt, handoffMemory)}
              disabled={busy}
            >
              Discuss
            </Button>
          )}
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
        </div>
      )}
    </div>
  );
}
