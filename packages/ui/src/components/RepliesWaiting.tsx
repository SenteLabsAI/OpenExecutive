"use client";

import { useCallback, useEffect, useState } from "react";

import Button, { buttonClass } from "@/components/ui/Button";
import {
  dismissReplyCard,
  editReplyText,
  getReplyCards,
  ReplySendError,
  sendReplyCard,
  type ReplyCard,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  relationLabel,
  replyFlagLines,
  mailboxName,
  safeGmailLink,
  sendLeftNothing,
  sendQuestion,
  senderLine,
  senderShort,
  draftIsLong,
} from "@/lib/replyCards";
import FeatureName from "@/components/FeatureName";

// Home: the replies the Executive wrote as you for mail that needs you
// (Delegate → Act as me → Draft replies to my inbox), shown as cards in
// "Needs you". Each card shows who wrote, what they wrote, the draft, what it
// leaves you to decide and anything to check. The reply waits on the card
// alone unless you chose to keep replies in your Drafts too. Edit changes its
// words right on the card (and in your Drafts when it's there:
// PUT /delegation/replies/{id}/text). Send (the card's primary) sends it from
// your own mailbox after you confirm who it goes to; Open in Gmail (when it's
// in your Drafts) and Dismiss are their own buttons beside it.
// GET /delegation/replies answers only the owner, so nothing shows for
// everyone else, on a backend without it, and when nothing is waiting.

// While a send Gmail hasn't confirmed is being settled (the card is
// "executing"), how often the cards are read again.
const SETTLE_POLL_MS = 20_000;

// The cards and their refresh, for the Home "Needs you" list, which shows
// each one as a card among the proposals. `cards` stays null for anyone but
// the owner, on a backend without the route, and until the first read.
export function useReplyCards() {
  const [cards, setCards] = useState<ReplyCard[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getReplyCards(controller.signal)
      .then(setCards)
      .catch(() => { /* no card is better than a broken one */ });
    return () => controller.abort();
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await getReplyCards();
      if (next) setCards(next);
    } catch {
      // Keep what's shown; the next look may work.
    }
  }, []);

  const settling = (cards ?? []).some((c) => c.status === "executing");
  useEffect(() => {
    if (!settling) return;
    const timer = setInterval(() => void refresh(), SETTLE_POLL_MS);
    return () => clearInterval(timer);
  }, [settling, refresh]);

  const gone = useCallback((decisionId: number, note?: string) => {
    setCards((prev) => (prev ?? []).filter((c) => c.decision_id !== decisionId));
    setNotice(note ?? null);
  }, []);

  // A card changed in place (its words edited).
  const replace = useCallback((card: ReplyCard) => {
    setCards((prev) => (prev ?? []).map((c) => (c.decision_id === card.decision_id ? card : c)));
  }, []);

  return { cards: cards ?? [], notice, gone, refresh, replace };
}

// What the Replies-waiting cards are, for the Needs you header's tip.
export const REPLIES_WAITING_TIP =
  "Mail that needs you, with a first reply the Executive wrote in your voice. Tap Edit to " +
  "change or add to it right here. Nothing is sent until you tap Send. If you keep replies " +
  "in your Drafts too, Dismiss deletes that draft unless you changed it in your mailbox. " +
  "Only you see these.";

// What the row is doing: idle, asking before sending (first or second
// time), or waiting on the backend.
// `allow`: Send + allow, in training (replies to them, or follow-ups to
// these people, go on their own after).
type Step =
  | { kind: "idle" }
  | { kind: "ask"; recipients: string[]; allow: boolean }
  | { kind: "confirm"; message: string; recipients: string[]; threadMovedOn: boolean; allow: boolean }
  | { kind: "busy"; label: string };

export function ReplyCardItem({
  card,
  onGone,
  onRefresh,
  onChanged,
  emphasized = false,
}: {
  card: ReplyCard;
  emphasized?: boolean;
  onGone: (id: number, note?: string) => void;
  onRefresh: () => Promise<void>;
  onChanged?: (card: ReplyCard) => void;
}) {
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);
  const [fullDraft, setFullDraft] = useState(false);
  // Edit: the draft's words in a box on the card, until Save or Cancel.
  const [editing, setEditing] = useState<string | null>(null);
  // Drafts in training: keep what you sent as an example when you changed it first.
  const [keepExample, setKeepExample] = useState(true);
  const learnsStyle = Boolean(card.learns_style);
  // Being settled: Gmail didn't confirm a send. The server says so, and the
  // section reads the cards again until it's settled.
  const unconfirmed = card.status === "executing";
  const relation = relationLabel(card.relation);
  const warnings = replyFlagLines(card.flags);
  const received = formatRelativeTime(card.received_at);
  const gmailLink = safeGmailLink(card.gmail_link);
  const mailbox = card.mailbox || mailboxName(card.gmail_link);
  // Kept in their Drafts too, or on this card alone until Send.
  const inMailbox = card.in_mailbox !== false;
  const who = card.from_name.trim() || card.from_email;
  const followUp = card.source === "follow_up";

  const dismiss = async () => {
    setStep({ kind: "busy", label: "Dismissing…" });
    setError(null);
    try {
      await dismissReplyCard(card.decision_id);
      onGone(card.decision_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't dismiss that reply.");
      setStep({ kind: "idle" });
    }
  };

  const saveEdit = async () => {
    if (editing === null) return;
    setStep({ kind: "busy", label: "Saving…" });
    setError(null);
    try {
      const updated = await editReplyText(card.decision_id, editing);
      onChanged?.(updated);
      setEditing(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save your change.");
    } finally {
      setStep({ kind: "idle" });
    }
  };

  const send = async (confirm: { recipients: string[]; thread_moved_on?: boolean }, allow: boolean) => {
    setStep({ kind: "busy", label: "Sending…" });
    setError(null);
    try {
      const result = await sendReplyCard(
        card.decision_id,
        {
          ...confirm,
          ...(allow ? { allow: true } : {}),
          ...(learnsStyle && keepExample ? { example: true } : {}),
        },
      );
      if (result.status === "sent") {
        onGone(
          card.decision_id,
          followUp
            ? allow
              ? `Sent your follow-up to ${who}. Follow-ups to them go on their own from now on.`
              : `Sent your follow-up to ${who}.`
            : allow
              ? `Sent your reply to ${who}. Replies to them go on their own from now on.`
              : `Sent your reply to ${who}.`,
        );
        return;
      }
      setStep({
        kind: "confirm",
        message: result.message,
        recipients: result.recipients,
        threadMovedOn: result.reasons.includes("thread_moved_on"),
        allow,
      });
    } catch (err) {
      const code = err instanceof ReplySendError ? err.code : "error";
      const message = err instanceof Error ? err.message : "Couldn't send that reply.";
      if (sendLeftNothing(code)) {
        onGone(card.decision_id, message);
        return;
      }
      setError(message);
      setStep({ kind: "idle" });
      if (code === "send_unconfirmed") void onRefresh();
    }
  };

  const busy = step.kind === "busy";
  // A long draft is clamped to keep the card short, but never while you are
  // confirming Send: what goes out is shown in full.
  const confirming = step.kind === "ask" || step.kind === "confirm";
  const clampDraft = draftIsLong(card.draft_body) && !fullDraft && !confirming;
  const label = "text-xs font-semibold uppercase tracking-wide text-fg-muted";
  return (
    <article
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
          {followUp ? "Follow-up waiting" : "Reply waiting"}
        </span>
        <FeatureName feature="act_as_me" className="text-[12px]" />
        {received && <span className="text-sm text-fg-subtle tabular-nums">{received}</span>}
      </div>
      <div className="text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
        {card.subject || "(no subject)"}
      </div>
      <div className="mt-1 text-sm text-fg-muted break-words">
        {followUp ? `Nobody answered your email to ${who}` : senderShort(card)}
        {[relation, card.sender_verified ? "" : "Address not verified"]
          .filter(Boolean)
          .map((part) => ` · ${part}`)
          .join("")}
      </div>

      {card.they_wrote && (
        <details className="mt-3 group">
          <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl border border-line px-3 text-sm font-medium text-fg-muted hover:bg-surface-overlay/60">
            <span aria-hidden="true" className="inline-block transition-transform group-open:rotate-90">▸</span>
            {followUp ? "What you wrote" : "What they wrote"}
          </summary>
          {!followUp && (
            <p className="mt-1.5 px-1 text-xs text-fg-subtle break-words">From: {senderLine(card)}</p>
          )}
          {/* Plain text: what a stranger wrote is never rendered as markup. */}
          <p className="mt-1.5 max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-xl border border-line bg-surface px-3 py-2 text-sm text-fg-muted">
            {card.they_wrote}
          </p>
        </details>
      )}

      <div className="mt-3">
        <div className={label}>Your draft</div>
        {editing !== null ? (
          <div className="mt-1.5">
            <div className="px-1 text-xs text-fg-subtle break-words">To: {card.draft_to.join(", ")}</div>
            <textarea
              aria-label="Your draft"
              rows={9}
              value={editing}
              disabled={busy}
              autoFocus
              onChange={(e) => setEditing(e.target.value)}
              className="mt-1 w-full resize-y whitespace-pre-wrap rounded-xl border border-line-strong bg-surface p-3 text-[15px] leading-relaxed text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50"
            />
            <p className="mt-1 px-1 text-xs text-fg-subtle">
              {inMailbox
                ? `Saving changes the draft in your ${mailbox} too. Nothing is sent until you tap Send.`
                : "Nothing is sent until you tap Send."}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button variant="primary" onClick={() => void saveEdit()} disabled={busy || !editing.trim()}>
                {busy ? step.label : "Save"}
              </Button>
              <Button variant="ghost" onClick={() => setEditing(null)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
        <div className="mt-1.5 rounded-xl border border-line bg-surface px-3 py-2">
          <div className="text-xs text-fg-subtle break-words">To: {card.draft_to.join(", ")}</div>
          <p
            className={`mt-1 whitespace-pre-wrap break-words text-sm text-fg ${
              clampDraft ? "line-clamp-4" : ""
            }`}
          >
            {card.draft_body}
          </p>
          {draftIsLong(card.draft_body) && !confirming && (
            <button
              type="button"
              onClick={() => setFullDraft((v) => !v)}
              aria-expanded={fullDraft}
              className="mt-1 min-h-10 text-sm font-medium text-accent cursor-pointer"
            >
              {fullDraft ? "Show less" : "Show full draft"}
            </button>
          )}
        </div>
        )}
      </div>

      {card.waited_because && (
        <p className="mt-3 text-xs text-fg-subtle break-words">Why it waited for you: {card.waited_because}</p>
      )}

      {card.open_questions.length > 0 && (
        <div className="mt-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-accent">Decide before sending</div>
          <ul className="mt-1 list-disc pl-5 space-y-0.5 text-sm text-fg">
            {card.open_questions.map((q, i) => (
              <li key={i} className="break-words">{q}</li>
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-sm text-amber-700 dark:text-amber-300">
          {warnings.map((w) => (
            <li key={w} className="break-words">⚠ {w}</li>
          ))}
        </ul>
      )}

      {unconfirmed ? (
        <p className="mt-3 text-sm text-fg-muted">
          {mailbox} hasn&apos;t confirmed this was sent. Check your Sent folder in {mailbox}; this card
          updates on its own within a few minutes.
        </p>
      ) : step.kind === "ask" || step.kind === "confirm" ? (
        <div className="mt-4 rounded-xl border border-accent/30 bg-accent/5 px-3.5 py-3">
          {step.kind === "confirm" && (
            <p className="text-sm text-amber-700 dark:text-amber-300">{step.message}</p>
          )}
          <p className="text-sm text-fg">{sendQuestion(step.recipients, mailbox, inMailbox)}</p>
          {step.allow && (
            <p className="mt-2 text-sm text-fg">
              From now on, {followUp ? "follow-ups" : "replies"} to {who} go on their own while Handle it for me is
              on. Topics that always wait, links and new people still wait for you.
            </p>
          )}
          {learnsStyle && (
            <label className="mt-2 flex min-h-touch items-center gap-2 text-sm text-fg">
              <input
                type="checkbox"
                checked={keepExample}
                onChange={(e) => setKeepExample(e.target.checked)}
                className="h-4 w-4 accent-accent"
              />
              Do it like this next time (if you changed it)
            </label>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              onClick={() =>
                void send(
                  step.kind === "confirm"
                    ? { recipients: step.recipients, thread_moved_on: step.threadMovedOn || undefined }
                    : { recipients: step.recipients },
                  step.allow,
                )
              }
            >
              {step.kind === "confirm" ? "Send anyway" : "Send now"}
            </Button>
            <Button variant="ghost" onClick={() => setStep({ kind: "idle" })}>
              Cancel
            </Button>
          </div>
        </div>
      ) : editing !== null ? null : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            onClick={() => setStep({ kind: "ask", recipients: card.draft_to, allow: false })}
            disabled={busy}
          >
            {busy ? step.label : "Send"}
          </Button>
          {card.can_allow && (
            <Button
              variant="secondary"
              onClick={() => setStep({ kind: "ask", recipients: card.draft_to, allow: true })}
              disabled={busy}
            >
              Send + allow
            </Button>
          )}
          <Button
            variant="secondary"
            onClick={() => {
              setError(null);
              setEditing(card.draft_body);
            }}
            disabled={busy}
          >
            Edit
          </Button>
          {gmailLink && (
            <a
              href={gmailLink}
              target="_blank"
              rel="noopener noreferrer"
              className={buttonClass("ghost")}
            >
              Open in {mailbox}
            </a>
          )}
          {/* Dismiss is its own button, set apart on the right, so it's never
              hunted for in a menu next to Send. */}
          <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy}>
            Dismiss
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}
