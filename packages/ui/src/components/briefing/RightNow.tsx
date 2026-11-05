"use client";

import { useEffect, useState } from "react";

import { ReplyCardItem, type useReplyCards } from "@/components/RepliesWaiting";
import Button, { buttonClass } from "@/components/ui/Button";
import { getMail, replyToMail, type MailView, type RightNowRow } from "@/lib/api";
import { clockLabel, rowTarget, rowWhen, type PointTarget } from "@/lib/rightNow";
import { mailboxName, safeGmailLink } from "@/lib/replyCards";

import { buildNarrativeSeed, type ContinueHandler } from "./shared";

// Right now's rows, shown in Home's Needs you list as one card each: what is
// waiting on the owner that has no card of its own (GET /today `right_now`): mail
// the Executive couldn't draft a reply to (never a conversation with a reply
// card in Needs you); drafts still in their own mailbox; chat messages asking them
// something. Built from the records on every load, never written by a
// model, so a row reads the same each time. An email row has View email and
// Reply (GET /delegation/mail, POST /delegation/mail/reply), a chat row
// opens the message in its app, and every row has a ✕ (POST
// /today/right-now/dismiss) that keeps it gone until something newer lands
// on it.

type Replies = ReturnType<typeof useReplyCards>;

// What a row's buttons need from Home.
export interface RowActions {
  replies?: Replies;
  onContinue?: ContinueHandler;
  onDismiss?: (row: RightNowRow) => void;
}

const KIND_ICON: Record<RightNowRow["kind"], string> = { mail: "✉", draft: "✎", chat: "💬" };
const KIND_LABEL: Record<RightNowRow["kind"], string> = { mail: "Email", draft: "Draft", chat: "Chat message" };

function DismissButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Dismiss: ${label}`}
      title="Dismiss"
      className="-mr-1 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-fg-muted transition hover:bg-accent/10 hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
    >
      <span aria-hidden className="text-base leading-none">✕</span>
    </button>
  );
}

function ListRow({ row, actions }: { row: RightNowRow; actions?: RowActions }) {
  const [open, setOpen] = useState<"view" | "reply" | null>(null);
  const target = rowTarget(row);
  const label = `${row.who} — ${row.subject}`;
  const when = rowWhen(row.at, new Date());
  const onContinue = actions?.onContinue;
  const talk = onContinue ? () => onContinue(buildNarrativeSeed(label)) : undefined;
  const mailTarget = target?.kind === "mail" && actions?.replies ? target : null;
  return (
    <li className="border-t border-line py-2.5 first:border-t-0">
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          className="mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-accent/5 text-sm text-fg-muted"
        >
          {KIND_ICON[row.kind]}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-[15px] text-fg">
            <span className="sr-only">{KIND_LABEL[row.kind]}: </span>
            <span className="font-semibold">{row.who}</span>
            <span className="text-fg-muted"> — {row.subject}</span>
          </p>
          {row.note && <p className="text-sm text-fg-muted">{row.note}</p>}
        </div>
        {when && <span className="flex-shrink-0 pt-1 text-xs tabular-nums text-fg-muted">{when}</span>}
        {actions?.onDismiss && <DismissButton label={label} onClick={() => actions.onDismiss?.(row)} />}
      </div>
      {mailTarget && actions?.replies && open ? (
        <div className="ml-11">
          <MailPanel
            target={mailTarget}
            replies={actions.replies}
            startWithReply={open === "reply"}
            onTalk={talk}
            onClose={() => setOpen(null)}
          />
        </div>
      ) : mailTarget ? (
        <div className="ml-11 mt-2 flex flex-wrap gap-2">
          <Button size="sm" onClick={() => setOpen("view")}>View email</Button>
          <Button size="sm" onClick={() => setOpen("reply")}>Reply</Button>
        </div>
      ) : target?.kind === "link" ? (
        <div className="ml-11 mt-2 flex flex-wrap gap-2">
          <Button size="sm" onClick={() => window.open(target.url, "_blank", "noopener,noreferrer")}>
            Open in {target.app}
          </Button>
        </div>
      ) : null}
    </li>
  );
}

/** One Right now row as its own card in Needs you's list. */
export function RightNowItem({ row, actions }: { row: RightNowRow; actions?: RowActions }) {
  return (
    <ul className="rounded-2xl border border-line bg-surface-elevated px-4 sm:px-5">
      <ListRow row={row} actions={actions} />
    </ul>
  );
}


type MailTarget = Extract<NonNullable<PointTarget>, { kind: "mail" }>;

// One email opened from its point: what they wrote, their files, what you
// last wrote to them, and the reply card for it (the one already open on
// the conversation, or one written when you tap Reply).
function MailPanel({
  target,
  replies,
  startWithReply,
  onTalk,
  onClose,
}: {
  target: MailTarget;
  replies: Replies;
  startWithReply: boolean;
  onTalk?: () => void;
  onClose: () => void;
}) {
  const [mail, setMail] = useState<MailView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cardId, setCardId] = useState<number | null>(target.cardId);
  const [writing, setWriting] = useState(false);
  const { refresh } = replies;
  const { threadId, messageId } = target;

  const reply = async () => {
    setWriting(true);
    setError(null);
    try {
      const id = await replyToMail(threadId, messageId);
      await refresh();
      setCardId(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't write that reply.");
    } finally {
      setWriting(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    getMail(threadId, messageId, controller.signal)
      .then((m) => {
        setMail(m);
        if (m.reply_card_id !== null) setCardId(m.reply_card_id);
        // Reply tapped on the point: write it once the email is open, unless
        // a card is already there.
        else if (startWithReply && target.cardId === null) void reply();
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Couldn't open that email.");
      });
    return () => controller.abort();
    // Opened once per email; `reply` and the flags are read as they were then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, messageId]);

  const card = cardId !== null ? replies.cards.find((c) => c.decision_id === cardId) : undefined;
  const link = mail ? safeGmailLink(mail.link) : "";
  const files = mail?.attachments ?? [];
  const who = mail ? mail.from_name.trim() || mail.from_email : "";
  const others = mail ? mail.cc.map((a) => a.split("@")[0]).join(", ") : "";
  return (
    <div className="ml-[18px] mt-2 rounded-xl border border-line bg-surface p-3">
      {!mail && !error && <p className="animate-pulse text-sm text-fg-muted">Opening the email…</p>}
      {mail && (
        <>
          <p className="text-sm">
            <span className="font-semibold text-fg">{who}</span>
            <span className="text-fg-muted">
              {" "}· to you{others ? `, cc ${others}` : ""}
              {clockLabel(mail.received_at) ? ` · ${clockLabel(mail.received_at)}` : ""}
            </span>
          </p>
          {mail.subject && <p className="mt-0.5 text-sm text-fg-muted">{mail.subject}</p>}
          <p className="mt-2 whitespace-pre-line break-words text-[15px] text-fg">{mail.text || "(No words, just the files.)"}</p>
          {(files.length > 0 || mail.has_attachments) && (
            <div className="mt-2 flex flex-wrap gap-2">
              {files.length > 0 ? (
                files.map((f, i) => (
                  <span key={`${i}-${f.name}`} className="rounded-lg border border-line px-2.5 py-1 text-sm text-fg">
                    📎 {f.name}
                  </span>
                ))
              ) : (
                <span className="text-sm text-fg-muted">📎 Has attachments</span>
              )}
            </div>
          )}
          {mail.their_last_words && (
            <p className="mt-2 text-sm text-fg-muted">
              Replying to you: &ldquo;{mail.their_last_words.replace(/\s+/g, " ").slice(0, 160)}
              {mail.their_last_words.length > 160 ? "…" : ""}&rdquo;
            </p>
          )}
        </>
      )}
      {card ? (
        <div className="mt-3">
          <ReplyCardItem
            card={card}
            onGone={(id, note) => {
              replies.gone(id, note);
              setCardId(null);
            }}
            onRefresh={replies.refresh}
            onChanged={replies.replace}
            defaultOpen
          />
        </div>
      ) : writing ? (
        <p className="mt-3 animate-pulse text-sm text-fg-muted">Writing a reply…</p>
      ) : null}
      {error && <p className="mt-2 text-sm text-rose-600 dark:text-rose-300">{error}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {!card && mail && !writing && (
          <Button size="sm" variant="primary" onClick={() => void reply()}>Reply</Button>
        )}
        {link && (
          <a href={link} target="_blank" rel="noopener noreferrer" className={buttonClass("secondary", "sm")}>
            Open in {mailboxName(link)}
          </a>
        )}
        {onTalk && <Button size="sm" onClick={onTalk}>Talk it through</Button>}
        <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
      </div>
    </div>
  );
}
