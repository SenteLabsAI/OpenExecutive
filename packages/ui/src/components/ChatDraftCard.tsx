"use client";

import { useCallback, useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  ReplySendError,
  changeChatDraft,
  deleteChatDraft,
  getChatDraft,
  sendChatDraft,
  setChatDraftText,
  type ChatDraft,
  type ChatDraftState,
} from "@/lib/api";
import { highlightPieces } from "@/lib/replyCards";

// The chat: an email the Executive wrote as you (Act as me), shown under its
// reply with who it goes to, its subject and its text, read from your Drafts
// (GET /delegation/drafts/{row}; until that answers, or on a backend without
// it, the start of the text as it was saved). Change it right here: "Edit the
// words myself", a tone pill, or "Tell me what to change…". Each rewrites
// this one card and the draft in your Drafts, and lightly marks the new
// sentences. Send sends that draft from your own mailbox; the server checks
// again first (the caller is you, Act as me on, the draft unchanged in your
// mailbox). Changed it there? Send it from there. A version a newer one in
// this conversation replaced folds to a line.

const PREVIEW_CHARS = 800; // what ghostwrite_email returns of the text

// Codes that mean there is nothing left to send from here.
const DONE: Record<string, string> = {
  already_sent: "Already sent.",
  draft_gone: "It isn't in your Drafts any more.",
  you_replied: "You've already replied in this conversation.",
};

// Codes after which the card reads the draft again: it moved on meanwhile.
const STALE = new Set(["draft_gone", "draft_changed", "already_sent", "already_handled"]);

// One tap each: what the pill asks the Executive to change.
const TONES: { label: string; ask: string }[] = [
  { label: "Softer", ask: "Make it softer and more friendly." },
  { label: "More professional", ask: "Make it more professional." },
  { label: "Shorter", ask: "Make it shorter." },
  { label: "Warmer", ask: "Make it warmer." },
  { label: "More direct", ask: "Make it more direct." },
];

function Who({ to, cc, subject }: { to: string[]; cc: string[]; subject: string }) {
  return (
    <dl className="mt-1 space-y-0.5 text-sm text-fg-muted break-words">
      <div>
        <dt className="inline">To: </dt>
        <dd className="inline text-fg">{to.join(", ") || "No one"}</dd>
      </div>
      {cc.length > 0 && (
        <div>
          <dt className="inline">Cc: </dt>
          <dd className="inline text-fg">{cc.join(", ")}</dd>
        </div>
      )}
      {subject && (
        <div>
          <dt className="inline">Subject: </dt>
          <dd className="inline text-fg">{subject}</dd>
        </div>
      )}
    </dl>
  );
}

// Home's Needs you opens a draft here too, with `canDelete` (a Delete that
// removes it from your Drafts) and `onFinished` (told once it is sent or
// isn't in your Drafts any more, so the row can go).
export default function ChatDraftCard({
  draft,
  canDelete = false,
  onFinished,
}: {
  draft: ChatDraft;
  canDelete?: boolean;
  onFinished?: () => void;
}) {
  const [now, setNow] = useState<ChatDraftState | null>(null);
  const [busy, setBusy] = useState(false);
  const [working, setWorking] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [ask, setAsk] = useState("");
  const [changed, setChanged] = useState<string[]>([]);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      setNow(await getChatDraft(draft.row));
    } catch {
      // Unreadable right now: the card keeps what it shows.
    }
  }, [draft.row]);

  useEffect(() => {
    const ctl = new AbortController();
    getChatDraft(draft.row, ctl.signal).then(
      (state) => {
        if (!ctl.signal.aborted) setNow(state);
      },
      () => {
        // Unreadable right now: the card keeps the text as it was saved.
      },
    );
    return () => ctl.abort();
  }, [draft.row]);

  const finished = Boolean(done) || now?.status === "sent" || now?.status === "gone";
  useEffect(() => {
    if (finished) onFinished?.();
  }, [finished, onFinished]);

  const failed = (err: unknown, fallback: string) => {
    const code = err instanceof ReplySendError ? err.code : "";
    setError(err instanceof Error ? err.message : fallback);
    if (STALE.has(code)) void load();
  };

  const remove = async () => {
    setBusy(true);
    setError(null);
    setConfirm(null);
    try {
      const state = await deleteChatDraft(draft.row);
      if (state.status === "gone") setDone("✓ Deleted from your Drafts.");
      else setNow(state);
    } catch (err) {
      failed(err, "Couldn't delete it.");
    } finally {
      setBusy(false);
    }
  };

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
        failed(err, "Couldn't send it.");
      }
    } finally {
      setBusy(false);
    }
  };

  const rewrite = async (label: string, run: () => Promise<ChatDraftState>) => {
    setBusy(true);
    setWorking(label);
    setError(null);
    setConfirm(null);
    try {
      const state = await run();
      setNow(state);
      setChanged(state.changed_sentences ?? []);
      setNote(state.note ?? "");
      setEditing(null);
      setAsk("");
    } catch (err) {
      failed(err, "Couldn't change the email.");
    } finally {
      setBusy(false);
      setWorking(null);
    }
  };

  const status = now?.status ?? "open";

  if (status === "replaced" || status === "gone" || status === "sent") {
    return (
      <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 opacity-70 sm:p-5">
        <div className="text-xs font-semibold uppercase tracking-wide text-accent">
          {status === "replaced" ? "Email as you · earlier version" : "Email as you"}
        </div>
        <Who to={draft.to} cc={[]} subject={draft.subject} />
        <p className="mt-2 text-sm text-fg-muted">
          {status === "replaced"
            ? "Replaced by a newer version."
            : status === "sent"
              ? "Sent."
              : "It isn't in your Drafts any more."}
        </p>
      </article>
    );
  }

  const open = now?.status === "open";
  const to = open ? now.to : draft.to;
  const cc = open ? now.cc : draft.cc;
  const subject = open ? now.subject : draft.subject;
  const body = open ? now.body : draft.preview;
  const files = open ? (now.files ?? []) : [];
  const cut = !open && draft.preview.length >= PREVIEW_CHARS;
  const theirs = now?.status === "changed";
  const canChange = open && !done && !theirs;

  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Email as you</div>
      <Who to={to} cc={cc} subject={subject} />
      {files.length > 0 && (
        <div className="mt-2">
          <div className="text-xs text-fg-muted">
            {files.length === 1 ? "1 file attached" : `${files.length} files attached`}
          </div>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {files.map((name, i) => (
              <li
                key={`${i}-${name}`}
                className="max-w-full truncate rounded-lg border border-line bg-surface px-2 py-1 text-xs text-fg"
                title={name}
              >
                {name}
              </li>
            ))}
          </ul>
        </div>
      )}
      {editing !== null ? (
        <div className="mt-2">
          <textarea
            aria-label="The email's words"
            value={editing}
            rows={Math.min(18, Math.max(6, editing.split("\n").length + 1))}
            maxLength={20000}
            disabled={busy}
            onChange={(e) => setEditing(e.target.value)}
            className="w-full resize-y rounded-xl border border-line-strong bg-surface p-3 text-sm leading-relaxed text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <p className="mt-1 px-1 text-xs text-fg-subtle">
            Saving changes the draft in your Drafts too. Nothing is sent until you tap Send.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              disabled={busy || !editing.trim()}
              onClick={() => void rewrite("Saving…", () => setChatDraftText(draft.row, editing))}
            >
              {working ?? "Save"}
            </Button>
            <Button variant="ghost" onClick={() => setEditing(null)} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        body && (
          <p className="mt-2 whitespace-pre-wrap text-sm text-fg break-words">
            {changed.length > 0
              ? highlightPieces(body, changed).map((piece, i) =>
                  piece.changed ? (
                    <mark key={i} className="rounded bg-accent/15 px-0.5 text-fg">
                      {piece.text}
                    </mark>
                  ) : (
                    <span key={i}>{piece.text}</span>
                  ),
                )
              : body}
            {cut && "…"}
          </p>
        )
      )}
      {cut && <p className="mt-1 text-xs text-fg-muted">The rest is in your Drafts.</p>}
      {note && editing === null && (
        <p className="mt-2 text-xs text-accent" role="status">
          {note}
        </p>
      )}
      {theirs && (
        <p className="mt-2 text-sm text-fg-muted">You changed this in your Drafts, so send it from there.</p>
      )}
      {done ? (
        <p className="mt-3 text-sm font-semibold text-emerald-700 dark:text-emerald-300" role="status">
          {done}
        </p>
      ) : (
        editing === null &&
        !theirs && (
          <>
            {confirm && (
              <p className="mt-3 text-sm text-fg" role="status">
                {confirm} Send it anyway?
              </p>
            )}
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button variant="primary" onClick={() => void send(confirm !== null)} disabled={busy}>
                {busy && !working ? "Sending…" : confirm ? "Send anyway" : "Send"}
              </Button>
              {canChange ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setError(null);
                    setEditing(now?.body ?? "");
                  }}
                  className="min-h-10 cursor-pointer px-2 text-sm font-medium text-accent disabled:opacity-50"
                >
                  Edit the words myself
                </button>
              ) : (
                now === null && (
                  <span className="text-xs text-fg-muted">Or change it in your Drafts and send it from there.</span>
                )
              )}
              {canDelete && canChange && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void remove()}
                  className="min-h-10 cursor-pointer px-2 text-sm font-medium text-rose-500 disabled:opacity-50"
                >
                  Delete
                </button>
              )}
            </div>
            {canChange && (
              <div className="mt-3 border-t border-line pt-3">
                <div className="flex flex-wrap gap-1.5">
                  {TONES.map((t) => (
                    <button
                      key={t.label}
                      type="button"
                      disabled={busy}
                      onClick={() => void rewrite(`${t.label}…`, () => changeChatDraft(draft.row, t.ask))}
                      className="min-h-9 cursor-pointer rounded-full border border-line-strong bg-surface px-3 text-xs font-medium text-fg hover:border-accent disabled:opacity-50"
                    >
                      {working === `${t.label}…` ? "Rewriting…" : t.label}
                    </button>
                  ))}
                </div>
                <form
                  className="mt-2 flex items-center gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const words = ask.trim();
                    if (words) void rewrite("Changing…", () => changeChatDraft(draft.row, words));
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
                      {working === "Changing…" ? "Changing…" : "Change"}
                    </Button>
                  )}
                </form>
              </div>
            )}
          </>
        )
      )}
      {error && (
        <p className="mt-2 text-sm text-rose-500" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}
