"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import ChatDraftCard from "@/components/ChatDraftCard";
import { ReplyCardItem, type useReplyCards } from "@/components/RepliesWaiting";
import Button, { buttonClass } from "@/components/ui/Button";
import {
  ReplySendError,
  deleteChatDraft,
  getMail,
  replyToMail,
  sendChatDraft,
  type MailView,
  type RightNowRow,
} from "@/lib/api";
import { clockLabel, draftRowId, rowTarget, rowWhen, type PointTarget } from "@/lib/rightNow";
import { mailboxName, safeGmailLink } from "@/lib/replyCards";

import { buildNarrativeSeed, type ContinueHandler } from "./shared";

// Right now's rows, shown in Home's Needs you list as one card each: what is
// waiting on the owner that has no card of its own (GET /today `right_now`): mail
// the Executive couldn't draft a reply to (never a conversation with a reply
// card in Needs you); drafts still in their own mailbox; chat messages asking them
// something. Built from the records on every load, never written by a
// model, so a row reads the same each time. An email row has View email and
// Reply (GET /delegation/mail, POST /delegation/mail/reply), a draft row has
// Send, See draft (the same card as under the chat, to change it first) and
// Delete (/delegation/drafts/{row}), a chat row
// opens the message in its app, and every row has a ✕ (POST
// /today/right-now/dismiss) that keeps it gone until something newer lands
// on it.

type Replies = ReturnType<typeof useReplyCards>;

// What a row's buttons need from Home.
export interface RowActions {
  replies?: Replies;
  onContinue?: ContinueHandler;
  onDismiss?: (row: RightNowRow) => void;
  // Drops a row that is done with (its draft sent or deleted) from the page.
  onGone?: (row: RightNowRow) => void;
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
  const draftId = draftRowId(row);
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
      ) : draftId !== null ? (
        <DraftActions row={row} draftId={draftId} onGone={actions?.onGone} />
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

// What a sent or deleted draft's row says before it goes.
const GONE_AFTER_MS = 2500;
const DRAFT_DONE: Record<string, string> = {
  already_sent: "Already sent.",
  draft_gone: "It isn't in your Drafts any more.",
  you_replied: "You've already replied in this conversation.",
};

// A draft row's buttons: Send and Delete right here, or See draft to read and
// change it first. Anything Send needs a second yes for, or a draft changed in
// the mailbox, opens the draft so its card can say so.
function DraftActions({
  row,
  draftId,
  onGone,
}: {
  row: RightNowRow;
  draftId: number;
  onGone?: (row: RightNowRow) => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<"send" | "delete" | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const leaving = useRef(false);
  const finish = useCallback(
    (line?: string) => {
      if (line) setDone(line);
      if (leaving.current) return;
      leaving.current = true;
      window.setTimeout(() => onGone?.(row), GONE_AFTER_MS);
    },
    [onGone, row],
  );

  const run = async (what: "send" | "delete") => {
    setBusy(what);
    setError(null);
    try {
      if (what === "send") {
        const result = await sendChatDraft(draftId);
        if (result.status === "confirm") {
          setOpen(true);
          return;
        }
        finish("✓ Sent from your mailbox.");
      } else {
        const state = await deleteChatDraft(draftId);
        if (state.status === "gone") finish("✓ Deleted from your Drafts.");
        else if (state.status === "sent") finish("Already sent.");
        else if (state.status === "replaced") finish("It isn't in your Drafts any more.");
        else setOpen(true); // still there: the card says why
      }
    } catch (err) {
      const code = err instanceof ReplySendError ? err.code : "";
      if (DRAFT_DONE[code]) finish(DRAFT_DONE[code]);
      else if (code === "draft_changed") setOpen(true);
      else setError(err instanceof Error ? err.message : "That didn't work. Try again.");
    } finally {
      setBusy(null);
    }
  };

  if (done) {
    return (
      <p className="ml-11 mt-2 text-sm font-semibold text-emerald-700 dark:text-emerald-300" role="status">
        {done}
      </p>
    );
  }
  if (open) {
    return (
      <div className="ml-11">
        <ChatDraftCard
          draft={{ row: draftId, to: [row.who], cc: [], subject: row.subject, preview: "" }}
          canDelete
          onFinished={finish}
        />
        <div className="mt-2">
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Close</Button>
        </div>
      </div>
    );
  }
  return (
    <div className="ml-11 mt-2">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="primary" disabled={busy !== null} onClick={() => void run("send")}>
          {busy === "send" ? "Sending…" : "Send"}
        </Button>
        <Button size="sm" disabled={busy !== null} onClick={() => setOpen(true)}>See draft</Button>
        <Button size="sm" disabled={busy !== null} onClick={() => void run("delete")}>
          <span className="text-rose-500">{busy === "delete" ? "Deleting…" : "Delete"}</span>
        </Button>
      </div>
      {error && <p className="mt-2 text-sm text-rose-500" role="alert">{error}</p>}
    </div>
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
