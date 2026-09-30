"use client";

import { useEffect, useState } from "react";

import InfoTip from "./InfoTip";
import { SectionHeading } from "./memories/shared";
import { dismissReplyCard, getReplyCards, type ReplyCard } from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import { relationLabel, replyFlagLines, safeGmailLink, senderLine } from "@/lib/replyCards";

// Today: the replies the Executive drafted in your own Gmail for mail that
// needs you (Settings → Act as me → Draft replies to my inbox). Each card
// shows who wrote, what they wrote, the draft, what it leaves you to decide
// and anything to check; you edit and send it in Gmail, or dismiss it here.
// GET /delegation/replies answers only the owner, so this hides itself for
// everyone else, on a backend without it, and when nothing is waiting.
export default function RepliesWaiting({ id }: { id?: string }) {
  const [cards, setCards] = useState<ReplyCard[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getReplyCards(controller.signal)
      .then(setCards)
      .catch(() => { /* no card is better than a broken one */ });
    return () => controller.abort();
  }, []);

  if (!cards || cards.length === 0) return null;
  const gone = (decisionId: number) =>
    setCards((prev) => (prev ?? []).filter((c) => c.decision_id !== decisionId));
  return (
    <section id={id} className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="flex items-center gap-1.5">
        <SectionHeading title="Replies waiting" count={cards.length} icon="mail" />
        <InfoTip align="left">
          Mail that needs you, with a first reply the Executive wrote in your voice. Each
          draft is in your Gmail Drafts: open it in Gmail to edit and send it. Nothing is
          sent for you. Dismiss deletes the draft, unless you&apos;ve edited it in Gmail.
          Only you see these.
        </InfoTip>
      </div>
      <div className="max-h-[40rem] overflow-y-auto pr-1 divide-y divide-line">
        {cards.map((card) => (
          <ReplyCardRow key={card.decision_id} card={card} onGone={gone} />
        ))}
      </div>
    </section>
  );
}

function ReplyCardRow({ card, onGone }: { card: ReplyCard; onGone: (id: number) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const relation = relationLabel(card.relation);
  const warnings = replyFlagLines(card.flags);
  const received = formatRelativeTime(card.received_at);
  const gmailLink = safeGmailLink(card.gmail_link);

  const dismiss = async () => {
    setBusy(true);
    setError(null);
    try {
      await dismissReplyCard(card.decision_id);
      onGone(card.decision_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't dismiss that reply.");
      setBusy(false);
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

      <div className="mt-2.5 flex flex-wrap items-center gap-3">
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
          disabled={busy}
          className="text-xs text-fg-muted hover:text-rose-300 transition-colors disabled:opacity-50"
        >
          {busy ? "Dismissing…" : "Dismiss"}
        </button>
      </div>
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </article>
  );
}
