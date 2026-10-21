"use client";

import { useState } from "react";

import Button from "@/components/ui/Button";
import { ReplySendError, sendChatDraft, type ChatDraft } from "@/lib/api";

// The chat: an email the Executive wrote as you (Act as me), shown under its
// reply with who it goes to, its subject and the start of its text, all as
// the draft was saved. Send sends that draft from your own mailbox, exactly
// as it was written; the server checks again first (the caller is you, Act as
// me on, the draft unchanged in your mailbox). Changed it there? Send it from
// there.

const PREVIEW_CHARS = 800; // what ghostwrite_email returns of the text

// Codes that mean there is nothing left to send from here.
const DONE: Record<string, string> = {
  already_sent: "Already sent.",
  draft_gone: "It isn't in your Drafts any more.",
  you_replied: "You've already replied in this conversation.",
};

export default function ChatDraftCard({ draft }: { draft: ChatDraft }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);

  const send = async (threadMovedOn = false) => {
    setBusy(true);
    setError(null);
    try {
      const result = await sendChatDraft(draft.row, threadMovedOn);
      if (result.status === "confirm") {
        setConfirm(result.message);
      } else {
        setConfirm(null);
        setDone("✓ Sent from your mailbox.");
      }
    } catch (err) {
      const code = err instanceof ReplySendError ? err.code : "";
      if (DONE[code]) {
        setConfirm(null);
        setDone(DONE[code]);
      } else {
        setError(err instanceof Error ? err.message : "Couldn't send it.");
      }
    } finally {
      setBusy(false);
    }
  };

  const to = draft.to.join(", ");
  const cut = draft.preview.length >= PREVIEW_CHARS;
  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Email as you</div>
      <dl className="mt-1 space-y-0.5 text-sm text-fg-muted break-words">
        <div>
          <dt className="inline">To: </dt>
          <dd className="inline text-fg">{to || "No one"}</dd>
        </div>
        {draft.cc.length > 0 && (
          <div>
            <dt className="inline">Cc: </dt>
            <dd className="inline text-fg">{draft.cc.join(", ")}</dd>
          </div>
        )}
        {draft.subject && (
          <div>
            <dt className="inline">Subject: </dt>
            <dd className="inline text-fg">{draft.subject}</dd>
          </div>
        )}
      </dl>
      {draft.preview && (
        <p className="mt-2 whitespace-pre-wrap text-sm text-fg break-words">
          {draft.preview}
          {cut && "…"}
        </p>
      )}
      {cut && <p className="mt-1 text-xs text-fg-muted">The rest is in your Drafts.</p>}
      {done ? (
        <p className="mt-3 text-sm font-semibold text-emerald-700 dark:text-emerald-300" role="status">
          {done}
        </p>
      ) : (
        <>
          {confirm && (
            <p className="mt-3 text-sm text-fg" role="status">
              {confirm} Send it anyway?
            </p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={() => void send(confirm !== null)} disabled={busy}>
              {busy ? "Sending…" : confirm ? "Send anyway" : "Send"}
            </Button>
            <span className="text-xs text-fg-muted">Or change it in your Drafts and send it from there.</span>
          </div>
        </>
      )}
      {error && (
        <p className="mt-2 text-sm text-rose-500" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}
