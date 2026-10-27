"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";

import Button, { buttonClass } from "@/components/ui/Button";
import {
  answerReplyQuestions,
  changeReply,
  dismissReplyCard,
  editReplyText,
  setReplyRecipients,
  getReplyCards,
  ReplySendError,
  sendReplyCard,
  type ReplyCard,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  cardGist,
  toDecideLabel,
  highlightPieces,
  questionsLeft,
  relationLabel,
  replyFlagLines,
  replyToChoice,
  mailboxName,
  safeGmailLink,
  sendLeftNothing,
  sendQuestion,
  senderLine,
  senderShort,
} from "@/lib/replyCards";
import FeatureName from "@/components/FeatureName";
import NextTimeTick from "@/components/NextTimeTick";

// Home: the replies the Executive wrote as you for mail that needs you
// (Delegate → Act as me → Draft replies to my inbox), shown as cards in
// "Needs you". Each card starts closed: who wrote, the Executive's line or
// two on what they asked and what the draft says, what's left to decide and
// one main button. Open, everything shows at once as a conversation: what
// they wrote; the Executive's word on the reply with what's left to decide
// (short answers to tap, or "Something else…" to type one) and what was
// decided (Change to pick again); any newer email in the conversation, which
// the Executive brought the reply up to date with, and what you asked it to
// change and what it did; then the reply itself with its Reply to choice,
// and "Tell me what to change…" (POST /delegation/replies/{id}/change). Each
// answer or change rewrites the draft (POST /delegation/replies/{id}/answer)
// and lightly marks the new sentences. "Edit the words myself" changes them
// right on the card (and in your Drafts when it's there: PUT
// /delegation/replies/{id}/text). The reply waits on the card alone unless
// you chose to keep replies in your Drafts too. Send waits until each
// question with answers to tap is answered, or you changed the words
// yourself, and sends it from your own mailbox after you confirm who it goes
// to; Open in Gmail (when it's in your Drafts) and Dismiss sit beside it.
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
  "Mail that needs you, with a first reply the Executive wrote in your voice. Tap an answer " +
  "under Decide before sending to write it in, or tap Edit to change it yourself. Nothing is " +
  "sent until you tap Send. If you keep replies " +
  "in your Drafts too, Dismiss deletes that draft unless you changed it in your mailbox. " +
  "Only you see these.";

// What the row is doing: idle, asking before sending (first or second
// time), or waiting on the backend.
// `allow`: Send with the "on its own next time" tick, in training (replies
// to them, or follow-ups to these people, go on their own after).
type Step =
  | { kind: "idle" }
  | { kind: "ask"; recipients: string[]; allow: boolean }
  | { kind: "confirm"; message: string; recipients: string[]; threadMovedOn: boolean; allow: boolean }
  | { kind: "busy"; label: string };

// The Executive's turn in a card's conversation.
function ExecBubble({ children }: { children: ReactNode }) {
  return (
    <div className="max-w-[92%] self-start rounded-2xl rounded-tl-md bg-accent/10 px-3 py-2 text-sm text-fg break-words">
      <div className="text-xs font-semibold text-accent">Executive</div>
      <div className="mt-0.5">{children}</div>
    </div>
  );
}

export function ReplyCardItem({
  card,
  onGone,
  onRefresh,
  onChanged,
  emphasized = false,
  defaultOpen = false,
}: {
  card: ReplyCard;
  emphasized?: boolean;
  // Start open (Right now's View email shows the reply straight away).
  defaultOpen?: boolean;
  onGone: (id: number, note?: string) => void;
  onRefresh: () => Promise<void>;
  onChanged?: (card: ReplyCard) => void;
}) {
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);
  // Closed until tapped: the summary, what's left to decide, one main button.
  const [open, setOpen] = useState(defaultOpen);
  // Edit: the draft's words in a box on the card, until Save or Cancel.
  const [editing, setEditing] = useState<string | null>(null);
  // Drafts in training: keep what you sent as an example when you changed it first.
  const [keepExample, setKeepExample] = useState(true);
  // "Something else…": which question's answer box is open, and its words.
  const [typing, setTyping] = useState<{ index: number; text: string } | null>(null);
  // "Tell me what to change…": the words in the box under the reply.
  const [ask, setAsk] = useState("");
  // Answered questions opened again with Change, to tap another answer.
  const [reopened, setReopened] = useState<number[]>([]);
  const questions = card.questions ?? [];
  const left = questionsLeft(card);
  const changed = card.changed_sentences ?? [];
  const learnsStyle = Boolean(card.learns_style);
  // Being settled: Gmail didn't confirm a send. The server says so, and the
  // section reads the cards again until it's settled.
  const unconfirmed = card.status === "executing";
  const relation = relationLabel(card.relation);
  const choice = replyToChoice(card);
  // The Reply to choice says who else was on it, so the warning doesn't.
  const warnings = replyFlagLines(choice ? card.flags.filter((f) => f !== "others_on_thread") : card.flags);
  const received = formatRelativeTime(card.received_at);
  const gmailLink = safeGmailLink(card.gmail_link);
  const mailbox = card.mailbox || mailboxName(card.gmail_link);
  // Kept in their Drafts too, or on this card alone until Send.
  const inMailbox = card.in_mailbox !== false;
  const who = card.from_name.trim() || card.from_email;
  const followUp = card.source === "follow_up";
  const firstName = card.from_name.trim().split(/\s+/)[0] || card.from_email;
  const [nextTime, setNextTime] = useState(false);

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

  const pickRecipients = async (everyone: boolean) => {
    if (!choice || everyone === choice.toEveryone) return;
    setStep({ kind: "busy", label: "Saving…" });
    setError(null);
    try {
      onChanged?.(await setReplyRecipients(card.decision_id, everyone));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't change who it goes to.");
    } finally {
      setStep({ kind: "idle" });
    }
  };

  const answer = async (index: number, text: string) => {
    const words = text.trim();
    if (!words) return;
    setStep({ kind: "busy", label: "Writing it in…" });
    setError(null);
    try {
      const updated = await answerReplyQuestions(card.decision_id, [
        { question_index: index, answer_text: words },
      ]);
      onChanged?.(updated);
      setTyping(null);
      setReopened((r) => r.filter((x) => x !== index));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't add that to your reply.");
    } finally {
      setStep({ kind: "idle" });
    }
  };

  const askChange = async () => {
    const words = ask.trim();
    if (!words) return;
    setStep({ kind: "busy", label: "Changing it…" });
    setError(null);
    try {
      onChanged?.(await changeReply(card.decision_id, words));
      setAsk("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't change your reply.");
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
          ...(card.caught_up_at ? { seen_caught_up_at: card.caught_up_at } : {}),
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
      // The reply may have been brought up to date with newer mail since it
      // was shown: show it as it is now, beside the second question.
      if (result.reasons.includes("thread_moved_on")) void onRefresh();
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
  const confirming = step.kind === "ask" || step.kind === "confirm";
  const label = "text-xs font-semibold uppercase tracking-wide text-fg-muted";
  // Closed, the card is its summary, what's left to decide and one main
  // button; it opens on a tap, and stays open while you edit or confirm.
  const expanded = open || confirming || editing !== null || typing !== null || ask.trim() !== "";
  const gist = cardGist(card);
  const toDecide = toDecideLabel(left);
  const replyToNow = choice ? (choice.toEveryone ? choice.everyone : choice.sender) : "";
  const tappable = questions.some((q) => q.choices.length > 0);
  // Open, everything shows at once, as a conversation: what they wrote, the
  // Executive's word on the reply with what's left to decide, anything that
  // came in or changed since, then the reply itself and a box to ask for a
  // change. What's left to decide sits with the Executive's latest word.
  const openQuestions = questions
    .map((q, i) => ({ q, i }))
    .filter(({ q, i }) => !(q.choices.length > 0 && q.answer) || reopened.includes(i));
  const answeredQuestions = questions
    .map((q, i) => ({ q, i }))
    .filter(({ q, i }) => q.choices.length > 0 && q.answer && !reopened.includes(i));
  const turns = card.conversation ?? [];
  const lastNote = turns.map((t) => t.kind).lastIndexOf("note");
  const nextTimeTick = (className: string) => card.can_allow ? (
    <NextTimeTick
      className={className}
      checked={nextTime}
      disabled={busy}
      onChange={setNextTime}
      label={followUp ? "Follow up with them on its own next time" : `Reply to ${firstName} on its own next time`}
      help="You can undo this in Handle it for me."
    />
  ) : null;
  const askToSend = () => {
    setOpen(true);
    setStep({ kind: "ask", recipients: card.draft_to, allow: Boolean(card.can_allow) && nextTime });
  };
  // Asking before Send shows the "next time" tick too (a closed card's Send
  // comes straight here), so what it says is read as it is now.
  const allowNow = step.kind === "ask" ? Boolean(card.can_allow) && nextTime : confirming && step.allow;
  const dismissButton = (
    // Dismiss is its own button, set apart on the right, so it's never
    // hunted for in a menu next to Send.
    <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy}>
      Dismiss
    </Button>
  );

  const renderQuestion = ({ q, i }: { q: (typeof questions)[number]; i: number }) => (
    <li key={i} className="text-sm text-fg">
      <div className="break-words">
        {q.answer && <span className="mr-1 text-accent" aria-label="Answered">✓</span>}
        {q.text}
      </div>
      {q.source && <div className="mt-0.5 text-xs text-fg-subtle break-words">{q.source}</div>}
      {q.answer && (
        <div className="mt-0.5 text-xs text-fg-muted break-words">Your answer: {q.answer}</div>
      )}
      {q.choices.length > 0 && editing === null && !unconfirmed && (
        <div className="mt-1.5 flex flex-wrap gap-2">
          {q.choices.map((c) => (
            <button
              key={c}
              type="button"
              disabled={busy}
              aria-pressed={q.answer === c}
              onClick={() => void answer(i, c)}
              className={`min-h-10 max-w-full rounded-full border px-3.5 py-1.5 text-left text-sm break-words transition-colors cursor-pointer disabled:opacity-50 ${
                q.answer === c
                  ? "border-accent bg-accent/10 text-fg"
                  : "border-line bg-surface text-fg-muted hover:border-line-strong hover:text-fg"
              }`}
            >
              {c}
            </button>
          ))}
          <button
            type="button"
            disabled={busy}
            aria-expanded={typing?.index === i}
            onClick={() => setTyping(typing?.index === i ? null : { index: i, text: "" })}
            className="min-h-10 rounded-full border border-dashed border-line px-3.5 py-1.5 text-sm text-fg-muted hover:border-line-strong hover:text-fg transition-colors cursor-pointer disabled:opacity-50"
          >
            Something else…
          </button>
        </div>
      )}
      {typing?.index === i && (
        <form
          className="mt-2 flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void answer(i, typing.text);
          }}
        >
          <input
            type="text"
            aria-label={`Your answer: ${q.text}`}
            value={typing.text}
            maxLength={500}
            autoFocus
            disabled={busy}
            onChange={(e) => setTyping({ index: i, text: e.target.value })}
            className="min-h-10 min-w-0 flex-1 basis-48 rounded-xl border border-line-strong bg-surface px-3 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50"
          />
          <Button type="submit" variant="secondary" disabled={busy || !typing.text.trim()}>
            Use this
          </Button>
        </form>
      )}
    </li>
  );

  const hasOpen = tappable ? openQuestions.length > 0 : card.open_questions.length > 0;
  const decideBlock = hasOpen ? (
    tappable ? (
      <ul className="mt-2 space-y-3">{openQuestions.map(renderQuestion)}</ul>
    ) : (
      <ul className="mt-1 list-disc space-y-0.5 pl-5">
        {card.open_questions.map((q, i) => (
          <li key={i} className="break-words">{q}</li>
        ))}
      </ul>
    )
  ) : null;
  const answeredLines = answeredQuestions.length > 0 ? (
    <ul className="mt-2 space-y-1">
      {answeredQuestions.map(({ q, i }) => (
        <li key={i} className="text-sm text-fg-muted break-words">
          <span className="mr-1 text-accent" aria-label="Answered">✓</span>
          {q.text} <span className="font-semibold text-fg">{q.answer}</span>
          {!unconfirmed && editing === null && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setReopened((r) => [...r, i])}
              className="ml-2 cursor-pointer text-accent disabled:opacity-50"
            >
              Change
            </button>
          )}
        </li>
      ))}
    </ul>
  ) : null;
  const wrote = followUp ? "I wrote a follow-up for you." : "I wrote your reply.";
  const intro = left > 1
    ? `${wrote} ${left} things to decide:`
    : left === 1
      ? `${wrote} One thing to decide:`
      : hasOpen
        ? `${wrote} Check these before sending:`
        : `${wrote} Read it over and send it when it looks right.`;
  return (
    <article
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized || expanded ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <button
        type="button"
        aria-expanded={expanded}
        disabled={busy || (expanded && (confirming || editing !== null || typing !== null))}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full cursor-pointer items-start gap-3 text-left disabled:cursor-default"
      >
        <span className="block min-w-0 flex-1">
          <span className="mb-2 flex items-center gap-2">
            <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
              {followUp ? "Follow-up waiting" : "Reply waiting"}
            </span>
            <FeatureName feature="act_as_me" className="text-[12px]" />
            {received && <span className="text-sm text-fg-subtle tabular-nums">{received}</span>}
          </span>
          <span className="block text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
            {card.subject || "(no subject)"}
          </span>
          <span className="mt-1 block text-sm text-fg-muted break-words">
            {followUp ? `Nobody answered your email to ${who}` : senderShort(card)}
            {[relation, card.sender_verified ? "" : "Address not verified"]
              .filter(Boolean)
              .map((part) => ` · ${part}`)
              .join("")}
          </span>
          {/* Plain text: the summary is written from a stranger's email too. */}
          {gist && <span className="mt-2 block text-[15px] leading-snug text-fg break-words">{gist}</span>}
        </span>
        <span className="flex shrink-0 items-center gap-1 pt-0.5 text-sm text-fg-muted">
          <span className="hidden sm:inline">{expanded ? "Close" : "Open"}</span>
          <span aria-hidden="true">{expanded ? "▴" : "▾"}</span>
        </span>
      </button>

      {!expanded ? (
        <>
          {(toDecide || replyToNow || warnings.length > 0) && !unconfirmed && (
            <div className="mt-2.5 flex flex-wrap gap-2">
              {toDecide && (
                <span className="rounded-full border border-accent/60 px-2.5 py-0.5 text-[13px] text-accent">{toDecide}</span>
              )}
              {replyToNow && (
                <span className="rounded-full border border-line-strong px-2.5 py-0.5 text-[13px] text-fg-muted">{replyToNow}</span>
              )}
              {warnings.length > 0 && (
                <span className="rounded-full border border-amber-500/50 px-2.5 py-0.5 text-[13px] text-amber-700 dark:text-amber-300">
                  ⚠ Check before sending
                </span>
              )}
            </div>
          )}
          {unconfirmed ? (
            <p className="mt-3 text-sm text-fg-muted">
              {mailbox} hasn&apos;t confirmed this was sent. Check your Sent folder in {mailbox}; this card
              updates on its own within a few minutes.
            </p>
          ) : (
            <>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {left > 0 ? (
                  <Button variant="primary" onClick={() => setOpen(true)} disabled={busy}>
                    Review
                  </Button>
                ) : (
                  <>
                    <Button variant="primary" onClick={askToSend} disabled={busy}>
                      Send
                    </Button>
                    <Button variant="secondary" onClick={() => setOpen(true)} disabled={busy}>
                      Review
                    </Button>
                  </>
                )}
                {dismissButton}
              </div>
            </>
          )}
        </>
      ) : (
        <>
      {card.waited_because && (
        <p className="mt-1 text-xs text-fg-subtle break-words">Waited for you: {card.waited_because}</p>
      )}
      <div className="mt-3 flex flex-col gap-2.5">
        {card.they_wrote && (
          <div className="max-w-[92%] self-start rounded-2xl rounded-tl-md bg-surface-overlay px-3 py-2">
            <div className="text-xs text-fg-subtle break-words">
              {followUp ? "You wrote" : senderLine(card)}
              {received ? ` · ${received}` : ""}
            </div>
            {/* Plain text: what a stranger wrote is never rendered as markup. */}
            <p className="mt-0.5 whitespace-pre-wrap break-words text-sm text-fg">{card.they_wrote}</p>
          </div>
        )}
        {turns.length === 0 ? (
          <ExecBubble>
            <p>{intro}</p>
            {decideBlock}
            {answeredLines}
          </ExecBubble>
        ) : (
          answeredQuestions.length > 0 && (
            <ExecBubble>
              <p>What you decided:</p>
              {answeredLines}
            </ExecBubble>
          )
        )}
        {turns.map((t, i) =>
          t.kind === "news" ? (
            <div
              key={i}
              className="max-w-[92%] self-start rounded-2xl rounded-tl-md border border-emerald-500/60 bg-surface-overlay px-3 py-2"
            >
              <div className="text-xs text-fg-subtle break-words">
                <span className="mr-1.5 font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">New</span>
                {(t.from_name || t.from_email || "").trim()}
                {t.at && formatRelativeTime(t.at) ? ` · ${formatRelativeTime(t.at)}` : ""}
              </div>
              <p className="mt-0.5 whitespace-pre-wrap break-words text-sm text-fg">{t.text}</p>
            </div>
          ) : t.kind === "asked" ? (
            <div key={i} className="max-w-[85%] self-end rounded-2xl rounded-tr-md bg-accent/20 px-3 py-2 text-sm text-fg break-words">
              {t.text}
            </div>
          ) : (
            <ExecBubble key={i}>
              <p>{t.text}</p>
              {i === lastNote && hasOpen && decideBlock}
            </ExecBubble>
          ),
        )}
        {busy && (step.label === "Writing it in…" || step.label === "Changing it…") && (
          <p className="text-xs text-fg-subtle" aria-live="polite">Writing it into your reply…</p>
        )}
      </div>
      <div className="mt-3 rounded-xl border border-line-strong bg-surface px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className={label}>Your reply</span>
          {choice && !unconfirmed && (
            <div
              role="radiogroup"
              aria-label="Reply to"
              className="ml-auto inline-flex overflow-hidden rounded-full border border-line-strong"
            >
              {([false, true] as const).map((everyone) => {
                const on = everyone === choice.toEveryone;
                return (
                  <button
                    key={String(everyone)}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    disabled={busy || confirming || editing !== null}
                    onClick={() => void pickRecipients(everyone)}
                    className={`min-h-9 px-3 text-[13px] disabled:opacity-60 ${
                      on ? "bg-accent/20 font-semibold text-fg" : "text-fg-muted hover:bg-surface-overlay/60"
                    }`}
                  >
                    {everyone ? choice.everyone : choice.sender}
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <div className="mt-1 text-xs text-fg-subtle break-words">To: {card.draft_to.join(", ")}</div>
        {editing !== null ? (
          <div className="mt-1.5">
            <textarea
              aria-label="Your reply"
              rows={9}
              value={editing}
              disabled={busy}
              autoFocus
              onChange={(e) => setEditing(e.target.value)}
              className="w-full resize-y whitespace-pre-wrap rounded-xl border border-line-strong bg-surface p-3 text-[15px] leading-relaxed text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
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
          <>
            <p className="mt-1.5 whitespace-pre-wrap break-words text-[15px] text-fg">
              {changed.length > 0
                ? highlightPieces(card.draft_body, changed).map((piece, i) =>
                    piece.changed ? (
                      <mark key={i} className="rounded bg-accent/15 px-0.5 text-fg">
                        {piece.text}
                      </mark>
                    ) : (
                      <span key={i}>{piece.text}</span>
                    ),
                  )
                : card.draft_body}
            </p>
            {!unconfirmed && !confirming && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setError(null);
                  setEditing(card.draft_body);
                }}
                className="mt-1 min-h-10 cursor-pointer text-sm font-medium text-accent disabled:opacity-50"
              >
                Edit the words myself
              </button>
            )}
          </>
        )}
      </div>
      {!unconfirmed && !confirming && editing === null && (
        <form
          className="mt-3 flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void askChange();
          }}
        >
          <input
            type="text"
            aria-label="Tell me what to change"
            placeholder="Tell me what to change…"
            value={ask}
            maxLength={500}
            disabled={busy}
            onChange={(e) => setAsk(e.target.value)}
            className="min-h-11 min-w-0 flex-1 rounded-full border border-line-strong bg-surface px-4 text-[15px] text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          {ask.trim() && (
            <Button type="submit" variant="secondary" disabled={busy}>
              Change
            </Button>
          )}
        </form>
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
          {step.kind === "ask" && nextTimeTick("mt-2")}
          {allowNow && (
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
                  allowNow,
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
        <>
        {left > 0 && (
          <p className="mt-4 text-xs text-fg-subtle">
            {left === 1
              ? "Answer the question above to send, or edit the words yourself."
              : `Answer the ${left} questions above to send, or edit the words yourself.`}
          </p>
        )}
        {nextTimeTick(left > 0 ? "mt-2" : "mt-4")}
        <div className={`${left > 0 || card.can_allow ? "mt-2" : "mt-4"} flex flex-wrap items-center gap-2`}>
          <Button
            variant="primary"
            onClick={askToSend}
            disabled={busy || left > 0}
          >
            {busy ? step.label : "Send"}
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
          {dismissButton}
        </div>
        </>
      )}
        </>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}
