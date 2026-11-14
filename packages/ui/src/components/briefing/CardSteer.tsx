"use client";

// A watch's card takes a reply that steers the watch ("that's our own repo,
// we just moved it"). The Executive adds it to the watch's What I know,
// which every later check reads before it tells anyone, closes this card
// (and any other card of the watch the reply also answers) and says what it
// changed. Undo puts the notes back and reopens the cards.

import Link from "next/link";
import { useState } from "react";

import { type SteerResult, steerAlert, undoSteer } from "@/lib/api";

const FIELD =
  "min-w-0 flex-1 resize-none [field-sizing:content] max-h-40 bg-transparent px-1 py-1 text-[15px] leading-snug text-fg placeholder:text-fg-subtle focus:outline-none disabled:opacity-50";

export default function CardSteer({
  alertId,
  watchSlug,
  onSteered,
  onUndone,
  autoFocus = false,
}: {
  alertId: number;
  watchSlug: string;
  autoFocus?: boolean;
  // The other cards it closed, so the page can drop them now.
  onSteered?: (result: SteerResult) => void;
  onUndone?: (result: SteerResult) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SteerResult | null>(null);
  const [undone, setUndone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fieldId = `card-steer-${alertId}`;

  const send = async () => {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await steerAlert(alertId, value);
      setResult(r);
      onSteered?.(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't send that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!result || busy) return;
    setBusy(true);
    setError(null);
    try {
      await undoSteer(result);
      setUndone(true);
      onUndone?.(result);
    } catch {
      setError("Couldn't undo that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    const watchHref = `/watchlist/${encodeURIComponent(result.watch_slug || watchSlug)}#what-i-know`;
    return (
      <div className="mt-3 rounded-xl border border-accent/25 bg-accent/5 px-3.5 py-3 text-[15px] leading-relaxed text-fg">
        <p className="whitespace-pre-wrap break-words">
          {undone ? "Undone. The watch is back as it was." : result.reply}
        </p>
        {!undone && (
          <div className="mt-2 text-sm text-fg-muted">
            <p className="font-semibold">What I know now</p>
            <p className="whitespace-pre-wrap break-words">{result.notes}</p>
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-4 text-sm font-semibold">
          <Link href={watchHref} className="text-accent hover:underline">
            Open the watch
          </Link>
          {!undone && (
            <button type="button" onClick={() => void undo()} disabled={busy} className="text-accent hover:underline disabled:opacity-50">
              {busy ? "Undoing…" : "Undo"}
            </button>
          )}
        </div>
        {error && (
          <p role="alert" className="mt-1 text-sm text-red-500">
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <form
      className="mt-3"
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <label htmlFor={fieldId} className="sr-only">
        Tell me what I got wrong
      </label>
      <div className="flex items-end gap-2 rounded-xl border border-line bg-surface px-2.5 py-2 focus-within:ring-2 focus-within:ring-accent/40">
        <textarea
          id={fieldId}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          rows={1}
          maxLength={500}
          autoFocus={autoFocus}
          disabled={busy}
          placeholder="Tell me what I got wrong, or what to watch for"
          className={FIELD}
        />
        <button
          type="submit"
          disabled={busy || !text.trim()}
          aria-label="Send"
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-accent text-white disabled:opacity-40"
        >
          {busy ? (
            <span aria-hidden="true" className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
          ) : (
            <span aria-hidden="true">↑</span>
          )}
        </button>
      </div>
      {error ? (
        <p role="alert" className="mt-1 text-sm text-red-500">
          {error}
        </p>
      ) : (
        <p className="mt-1 text-xs text-fg-subtle">I&apos;ll fix the watch and remember it.</p>
      )}
    </form>
  );
}
