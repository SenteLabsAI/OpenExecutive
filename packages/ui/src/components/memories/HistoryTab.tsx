"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import SidePanel from "@/components/ui/SidePanel";
import {
  forgetHistoryConversation,
  forgetHistoryNote,
  getHistory,
  updateHistoryNote,
  type HistoryNote,
  type HistoryState,
} from "@/lib/api";
import { kindLabel, noteExpiry, noteText, noteWhere } from "@/lib/history";
import { EmptyState, formatDate } from "./shared";

// History — Always in the loop. The signed-in person's own private notes of
// what they said in replies they approved and sent. Only they see them (the
// owner included); here they correct, pin and forget them, or ask the
// Executive not to remember a conversation at all.

const SEARCH_DELAY_MS = 300;

export default function HistoryTab({
  onCount,
  onAvailable,
}: {
  onCount: (n: number | null) => void;
  /** false when this viewer has no notes to see; the tab then goes away. */
  onAvailable: (available: boolean) => void;
}) {
  const [state, setState] = useState<HistoryState | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [correcting, setCorrecting] = useState<HistoryNote | null>(null);
  // Only the newest read may land: an older one (a reload after a pin, a
  // search the person has since typed past) must not overwrite it.
  const latest = useRef(0);

  const refresh = useCallback(
    async (q: string, signal?: AbortSignal) => {
      const mine = ++latest.current;
      setFailed(false);
      try {
        const next = await getHistory(q, signal);
        if (mine !== latest.current) return;
        onAvailable(next !== null);
        setState(next);
        // The badge counts every note, not just the ones a search found.
        if (!q.trim()) onCount(next ? next.notes.length : null);
      } catch (err) {
        if ((err as Error)?.name === "AbortError" || mine !== latest.current) return;
        setFailed(true);
        onCount(null);
      } finally {
        setLoading(false);
      }
    },
    [onCount, onAvailable],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => void refresh(query, ctrl.signal), query ? SEARCH_DELAY_MS : 0);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [query, refresh]);

  const reload = useCallback(() => refresh(query), [refresh, query]);

  const act = useCallback(
    async (run: () => Promise<unknown>, failure: string) => {
      try {
        await run();
      } catch (err) {
        window.alert(err instanceof Error ? err.message : failure);
        return;
      }
      void reload();
    },
    [reload],
  );

  if (loading) return <div className="text-fg-muted text-[15px] py-4">Loading…</div>;
  const failure = (
    <div className="text-fg-muted text-sm py-3">
      Couldn&apos;t load your notes.{" "}
      <button onClick={() => void reload()} className="font-medium text-accent hover:underline">
        Try again
      </button>
    </div>
  );
  // A failed first read has nothing to show; a later one keeps the search box
  // and the notes already on screen.
  if (!state) return failed ? failure : null;

  const empty = state.notes.length === 0 && !query.trim();
  return (
    <div className="py-3">
      {empty ? (
        <HistoryEmpty state={state} />
      ) : (
        <>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search your notes"
            aria-label="Search your notes"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          {failed && failure}
          {state.notes.length === 0 ? (
            <EmptyState message="No notes match." />
          ) : (
            <div className="mt-1 divide-y divide-line">
              {state.notes.map((note) => (
                <NoteRow
                  key={note.id}
                  note={note}
                  onCorrect={() => setCorrecting(note)}
                  onPin={() =>
                    void act(() => updateHistoryNote(note.id, { pinned: !note.pinned }), "Couldn't change the note.")
                  }
                  onForget={() => {
                    if (!window.confirm("Forget this note? This cannot be undone.")) return;
                    void act(() => forgetHistoryNote(note.id), "Couldn't forget the note.");
                  }}
                  onDontRemember={() => {
                    if (
                      !window.confirm(
                        "Forget every note from this conversation, and never note it again? This cannot be undone.",
                      )
                    )
                      return;
                    void act(() => forgetHistoryConversation(note.id), "Couldn't forget the conversation.");
                  }}
                />
              ))}
            </div>
          )}
          <p className="mt-3 text-xs text-fg-subtle">
            Only you see these. New notes are kept {retentionPhrase(state.effective_retention_days)}.
          </p>
        </>
      )}
      {/* Keyed by note, so the box starts from the note it opened on. */}
      <CorrectPanel
        key={correcting?.id ?? "closed"}
        note={correcting}
        onClose={() => setCorrecting(null)}
        onSave={async (correction) => {
          const note = correcting;
          if (!note) return;
          try {
            await updateHistoryNote(note.id, { correction });
          } catch (err) {
            window.alert(err instanceof Error ? err.message : "Couldn't change the note.");
            return;
          }
          setCorrecting(null);
          void reload();
        }}
      />
    </div>
  );
}

function retentionPhrase(days: number | null): string {
  return days === null ? "until you forget them" : days === 365 ? "for a year" : `for ${days} days`;
}

function HistoryEmpty({ state }: { state: HistoryState }) {
  if (state.reply_notes) {
    return (
      <EmptyState message="No notes yet. They're added after you send a reply the Executive drafted for you." />
    );
  }
  return (
    <div className="text-center py-14 px-4 text-fg-muted text-[15px] leading-relaxed">
      <p>
        Keep track of what happens is off. When it&apos;s on, the Executive keeps private notes of what
        you said in replies you approve and send, so it can remind you later.
      </p>
      {state.can_note_replies && (
        <Link href="/settings/act-as-me" className="mt-3 inline-block font-medium text-accent hover:underline">
          Turn it on in Act as me
        </Link>
      )}
    </div>
  );
}

function NoteRow({
  note,
  onCorrect,
  onPin,
  onForget,
  onDontRemember,
}: {
  note: HistoryNote;
  onCorrect: () => void;
  onPin: () => void;
  onForget: () => void;
  onDontRemember: () => void;
}) {
  const corrected = Boolean(note.correction?.trim());
  return (
    <div className="py-3.5">
      <div className="flex items-center justify-between gap-3 mb-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg-muted">
          <span className="px-2 py-0.5 rounded-lg bg-surface-overlay text-fg font-medium">{kindLabel(note.kind)}</span>
          {/* The name is theirs to choose; the address says who it really was. */}
          <span className="truncate" title={note.counterpart}>
            {noteWhere(note)}
          </span>
          <span>{formatDate(note.occurred_at)}</span>
          {note.pinned && <span className="text-xs font-medium text-accent">Pinned</span>}
        </div>
        <OverflowMenu
          size="sm"
          label="Note actions"
          items={[
            { label: "Correct", onSelect: onCorrect },
            { label: note.pinned ? "Unpin" : "Pin", onSelect: onPin },
            { label: "Don't remember this", onSelect: onDontRemember },
            { label: "Forget", danger: true, onSelect: onForget },
          ]}
        />
      </div>
      <div className="space-y-1">
        <div className="text-[15px] text-fg">
          {noteText(note)}
          {corrected && <span className="ml-1 text-xs text-fg-subtle">(as you corrected it)</span>}
        </div>
        {note.quote && (
          <div className="text-sm text-fg-muted line-clamp-3" title={note.quote}>
            Your words: “{note.quote}”
          </div>
        )}
        <div className="text-xs text-fg-subtle">
          {note.due_date ? `Due ${note.due_date} · ` : ""}
          {noteExpiry(note)}
        </div>
      </div>
    </div>
  );
}

function CorrectPanel({
  note,
  onClose,
  onSave,
}: {
  note: HistoryNote | null;
  onClose: () => void;
  onSave: (correction: string) => Promise<void>;
}) {
  const [text, setText] = useState(() => (note ? noteText(note) : ""));
  const [busy, setBusy] = useState(false);

  const save = async (correction: string) => {
    setBusy(true);
    try {
      await onSave(correction);
    } finally {
      setBusy(false);
    }
  };

  const corrected = Boolean(note?.correction?.trim());
  return (
    <SidePanel
      open={note !== null}
      onClose={onClose}
      title="Correct this note"
      subtitle={note ? `${noteWhere(note)} · ${formatDate(note.occurred_at)}` : undefined}
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          {corrected && (
            <Button variant="ghost" onClick={() => void save("")} disabled={busy}>
              Use the original
            </Button>
          )}
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void save(text.trim())} disabled={busy || !text.trim()}>
            Save
          </Button>
        </div>
      }
    >
      {note && (
        <div className="space-y-4">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            maxLength={400}
            aria-label="What the note should say"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <div className="text-sm text-fg-muted space-y-1">
            {note.counterpart && (
              <div className="break-all">
                <span className="text-fg-subtle">With: </span>
                {note.counterpart}
              </div>
            )}
            <div>
              <span className="text-fg-subtle">Noted as: </span>
              {note.summary}
            </div>
            {note.quote && (
              <div>
                <span className="text-fg-subtle">Your words: </span>“{note.quote}”
              </div>
            )}
          </div>
          <p className="text-xs text-fg-subtle">
            The Executive uses your version from now on. Your words stay as you wrote them.
          </p>
        </div>
      )}
    </SidePanel>
  );
}
