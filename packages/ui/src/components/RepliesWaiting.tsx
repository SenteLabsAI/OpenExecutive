"use client";

import { useCallback, useEffect, useState } from "react";

import InfoTip from "./InfoTip";
import { SectionHeading } from "./memories/shared";
import {
  dismissReplyCard,
  getReplyCards,
  ReplySendError,
  sendReplyCard,
  type ReplyCard,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  relationLabel,
  replyFlagLines,
  safeGmailLink,
  sendLeftNothing,
  sendQuestion,
  senderLine,
} from "@/lib/replyCards";

// Today: the replies the Executive drafted in your own Gmail for mail that
// needs you (Settings → Act as me → Draft replies to my inbox). Each card
// shows who wrote, what they wrote, the draft, what it leaves you to decide
// and anything to check. Send sends that draft from your Gmail exactly as it
// is there, after you confirm who it goes to; Edit in Gmail opens it there;
// Dismiss deletes it unless you edited it. GET /delegation/replies answers
// only the owner, so this hides itself for everyone else, on a backend
// without it, and when nothing is waiting.

// While a send Gmail hasn't confirmed is being settled (the card is
// "executing"), how often the cards are read again.
const SETTLE_POLL_MS = 20_000;

export default function RepliesWaiting({ id }: { id?: string }) {
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

  if (!cards || (cards.length === 0 && !notice)) return null;
  const gone = (decisionId: number, note?: string) => {
    setCards((prev) => (prev ?? []).filter((c) => c.decision_id !== decisionId));
    setNotice(note ?? null);
  };
  return (
    <section id={id} className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="flex items-center gap-1.5">
        <SectionHeading title="Replies waiting" count={cards.length} icon="mail" />
        <InfoTip align="left">
          Mail that needs you, with a first reply the Executive wrote in your voice. Each
          draft is in your Gmail Drafts, and nothing is sent until you tap Send: it sends that
          draft exactly as it is in Gmail, so edit it there first if you want to change it.
          Dismiss deletes the draft, unless you&apos;ve edited it in Gmail. Only you see these.
        </InfoTip>
      </div>
      {notice && <p className="mb-2 text-xs text-emerald-300">{notice}</p>}
      <div className="max-h-[40rem] overflow-y-auto pr-1 divide-y divide-line">
        {cards.map((card) => (
          <ReplyCardRow key={card.decision_id} card={card} onGone={gone} onRefresh={refresh} />
        ))}
      </div>
    </section>
  );
}

// What the row is doing: idle, asking before sending (first or second
// time), or waiting on the backend.
type Step =
  | { kind: "idle" }
  | { kind: "ask"; recipients: string[] }
  | { kind: "confirm"; message: string; recipients: string[]; threadMovedOn: boolean }
  | { kind: "busy"; label: string };

function ReplyCardRow({
  card,
  onGone,
  onRefresh,
}: {
  card: ReplyCard;
  onGone: (id: number, note?: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);
  // Being settled: Gmail didn't confirm a send. The server says so, and the
  // section reads the cards again until it's settled.
  const unconfirmed = card.status === "executing";
  const relation = relationLabel(card.relation);
  const warnings = replyFlagLines(card.flags);
  const received = formatRelativeTime(card.received_at);
  const gmailLink = safeGmailLink(card.gmail_link);
  const who = card.from_name.trim() || card.from_email;

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

  const send = async (confirm: { recipients: string[]; thread_moved_on?: boolean }) => {
    setStep({ kind: "busy", label: "Sending…" });
    setError(null);
    try {
      const result = await sendReplyCard(card.decision_id, confirm);
      if (result.status === "sent") {
        onGone(card.decision_id, `Sent your reply to ${who}.`);
        return;
      }
      setStep({
        kind: "confirm",
        message: result.message,
        recipients: result.recipients,
        threadMovedOn: result.reasons.includes("thread_moved_on"),
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

  return (
    <article className="py-3 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm text-fg break-words">{senderLine(card)}</div>
          <div className="text-xs text-fg-muted mt-0.5">
            {[relation, card.sender_verified ? "" : "Address not verified"].filter(Boolean).join(" · ")}
          </div>
        </div>
        {received && <span className="flex-shrink-0 text-xs text-fg-subtle tabular-nums">{received}</span>}
      </div>
      <div className="mt-1.5 text-sm font-medium text-fg break-words">{card.subject || "(no subject)"}</div>

      {card.they_wrote && (
        <details className="mt-2 group">
          <summary className="cursor-pointer list-none text-[10px] font-semibold uppercase tracking-wide text-fg-muted">
            <span className="inline-block transition-transform group-open:rotate-90">▸</span> They wrote
          </summary>
          {/* Plain text: what a stranger wrote is never rendered as markup. */}
          <p className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-line bg-surface px-2 py-1.5 text-xs text-fg-muted">
            {card.they_wrote}
          </p>
        </details>
      )}

      <div className="mt-2">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-fg-muted">Your draft</div>
        <div className="mt-1 rounded-md border border-line bg-surface px-2 py-1.5">
          <div className="text-[11px] text-fg-subtle break-words">To: {card.draft_to.join(", ")}</div>
          <p className="mt-1 whitespace-pre-wrap break-words text-xs text-fg">{card.draft_body}</p>
        </div>
      </div>

      {card.open_questions.length > 0 && (
        <div className="mt-2">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-indigo-300">
            Decide before sending
          </div>
          <ul className="mt-1 list-disc pl-4 space-y-0.5 text-xs text-fg">
            {card.open_questions.map((q, i) => (
              <li key={i} className="break-words">{q}</li>
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-amber-300">
          {warnings.map((w) => (
            <li key={w} className="break-words">⚠ {w}</li>
          ))}
        </ul>
      )}

      {unconfirmed ? (
        <p className="mt-2.5 text-xs text-fg-muted">
          Gmail hasn&apos;t confirmed this was sent. Check your Sent folder in Gmail; this card
          updates on its own within a few minutes.
        </p>
      ) : step.kind === "ask" || step.kind === "confirm" ? (
        <div className="mt-2.5 rounded-md border border-indigo-500/30 bg-indigo-500/5 px-2.5 py-2">
          {step.kind === "confirm" && <p className="text-xs text-amber-300">{step.message}</p>}
          <p className="text-xs text-fg">{sendQuestion(step.recipients)}</p>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() =>
                void send(
                  step.kind === "confirm"
                    ? { recipients: step.recipients, thread_moved_on: step.threadMovedOn || undefined }
                    : { recipients: step.recipients },
                )
              }
              className="rounded-md bg-indigo-500 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-400"
            >
              {step.kind === "confirm" ? "Send anyway" : "Send now"}
            </button>
            <button
              type="button"
              onClick={() => setStep({ kind: "idle" })}
              className="text-xs text-fg-muted hover:text-fg"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-2.5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => setStep({ kind: "ask", recipients: card.draft_to })}
            disabled={step.kind === "busy"}
            className="rounded-md bg-indigo-500 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
          >
            {step.kind === "busy" ? step.label : "Send"}
          </button>
          {gmailLink && (
            <a
              href={gmailLink}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-indigo-400 hover:text-indigo-300"
            >
              Edit in Gmail ↗
            </a>
          )}
          <button
            type="button"
            onClick={() => void dismiss()}
            disabled={step.kind === "busy"}
            className="text-xs text-fg-muted hover:text-rose-300 transition-colors disabled:opacity-50"
          >
            Dismiss
          </button>
        </div>
      )}
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </article>
  );
}
