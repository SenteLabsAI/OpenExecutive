"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import { listMemoryChanges, undoMemoryChange, type MemoryChange } from "@/lib/api";
import { changeCounts, changeDetail, changeHref, changeKey } from "@/lib/memoryChanges";

// What it remembered lately (GET /memories/changes), shown twice: the chip
// under a chat reply, and "New since your last visit" on the Memory page.
// Each row can be taken back with Undo, through its own list's route.

type RowState = "idle" | "busy" | "undone" | "failed";

function useUndo() {
  const [state, setState] = useState<Record<string, RowState>>({});
  const undo = async (change: MemoryChange) => {
    const key = changeKey(change);
    setState((s) => ({ ...s, [key]: "busy" }));
    try {
      await undoMemoryChange(change);
      setState((s) => ({ ...s, [key]: "undone" }));
    } catch {
      setState((s) => ({ ...s, [key]: "failed" }));
    }
  };
  return { state, undo };
}

function UndoButton({ change, state, onUndo }: { change: MemoryChange; state: RowState; onUndo: () => void }) {
  if (state === "undone") return <span className="text-fg-muted">Undone</span>;
  if (!change.can_undo) return null;
  return (
    <button
      type="button"
      onClick={onUndo}
      disabled={state === "busy"}
      className="font-semibold text-accent hover:underline disabled:opacity-60 cursor-pointer"
    >
      {state === "busy" ? "Undoing…" : state === "failed" ? "Couldn't undo. Try again" : "Undo"}
    </button>
  );
}

/** Under a chat reply: "✓ Memory updated · 1 lesson", opening to the rows.
 * Looks a few times after the reply, since some of what it keeps is worked
 * out just after; a later turn doesn't stop those looks, and `seen` puts
 * each change on the first chip that finds it. Shows nothing when nothing
 * was kept. */
export function MemoryUpdatedChip({
  sessionId,
  since,
  seen,
  onShown,
}: {
  sessionId: string;
  since: string;
  /** What an earlier chip in this chat already showed. */
  seen: Set<string>;
  /** Called once it shows a fact or decision (the chat then drops its own fact chip). */
  onShown?: () => void;
}) {
  const [changes, setChanges] = useState<MemoryChange[]>([]);
  const [open, setOpen] = useState(false);
  const { state, undo } = useUndo();
  // What this chip itself showed, so a later look keeps it.
  const mine = useRef(new Set<string>());

  useEffect(() => {
    let cancelled = false;
    const look = () => {
      listMemoryChanges(since, sessionId)
        .then((rows) => {
          if (cancelled || !rows) return;
          const shown = rows.filter((c) => mine.current.has(changeKey(c)) || !seen.has(changeKey(c))).reverse();
          shown.forEach((c) => {
            mine.current.add(changeKey(c));
            seen.add(changeKey(c));
          });
          // Keep what it already showed: an undone row is gone from the
          // server's answer but stays here, struck through.
          setChanges((prev) => {
            const had = new Set(prev.map(changeKey));
            return [...prev, ...shown.filter((c) => !had.has(changeKey(c)))];
          });
        })
        .catch(() => {});
    };
    const timers = [2_000, 8_000, 20_000, 45_000].map((ms) => window.setTimeout(look, ms));
    return () => {
      cancelled = true;
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, [sessionId, since, seen]);

  // Only a fact or decision row stands in for the chat's own fact chip.
  const hasFact = changes.some((c) => c.kind === "fact" || c.kind === "decision");
  useEffect(() => {
    if (hasFact) onShown?.();
  }, [hasFact, onShown]);

  if (changes.length === 0) return null;
  const left = changes.filter((c) => state[changeKey(c)] !== "undone");
  return (
    <div className="mt-2 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex min-h-[36px] items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-sm cursor-pointer hover:bg-emerald-500/15 transition-colors"
      >
        <Icon name="check" size="w-4 h-4" className="text-emerald-700 dark:text-emerald-300" />
        <span className="font-semibold text-emerald-700 dark:text-emerald-300">Memory updated</span>
        {left.length > 0 && <span className="text-emerald-700/80 dark:text-emerald-300/80">· {changeCounts(left)}</span>}
      </button>
      {open && (
        <ul className="max-w-xl rounded-2xl border border-line bg-surface-overlay/60 px-4 py-1">
          {changes.map((c) => (
            <li key={changeKey(c)} className="py-3 border-t border-line first:border-t-0 min-w-0">
              <div
                className={`text-[15px] font-semibold break-words ${state[changeKey(c)] === "undone" ? "text-fg-muted line-through" : "text-fg"}`}
              >
                {c.title}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-sm text-fg-muted">
                <span>{chipCaption(c)}</span>
                {c.can_undo && <span aria-hidden>·</span>}
                <UndoButton change={c} state={state[changeKey(c)] ?? "idle"} onUndo={() => void undo(c)} />
                <span aria-hidden>·</span>
                <Link href={changeHref(c)} className="font-semibold text-accent hover:underline">
                  See in Memory
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function chipCaption(c: MemoryChange): string {
  if (c.status === "proposed") return "Waiting for your OK";
  if (c.kind === "worked_out") return c.source ? `Worked out from ${c.source}` : "Worked out from what it read";
  if (c.kind === "lesson") return "Learned from this chat";
  return "From what you just said";
}

const PREVIEW = 3;

/** Top of Memory → What it knows: what was kept since the last visit. */
export function NewSinceCard({ since }: { since: string | null }) {
  const [changes, setChanges] = useState<MemoryChange[] | null>(null);
  const [all, setAll] = useState(false);
  const { state, undo } = useUndo();

  useEffect(() => {
    if (!since) return;
    let cancelled = false;
    listMemoryChanges(since)
      .then((rows) => {
        if (!cancelled) setChanges(rows ?? []);
      })
      .catch(() => {
        if (!cancelled) setChanges([]);
      });
    return () => {
      cancelled = true;
    };
  }, [since]);

  if (!changes || changes.length === 0) return null;
  const shown = all ? changes : changes.slice(0, PREVIEW);
  return (
    <section className="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5 sm:p-6 min-w-0">
      <h2 className="text-base sm:text-lg font-semibold text-fg">New since your last visit · {changes.length}</h2>
      <ul className="mt-2">
        {shown.map((c) => {
          const rowState = state[changeKey(c)] ?? "idle";
          return (
            <li key={changeKey(c)} className="py-2.5 flex items-start justify-between gap-3 min-w-0">
              <div className="min-w-0">
                <div
                  className={`text-[15px] font-semibold break-words ${rowState === "undone" ? "text-fg-muted line-through" : "text-fg"}`}
                >
                  {c.title}
                </div>
                <div className="text-sm text-fg-muted mt-0.5 break-words">
                  <Link href={changeHref(c)} className="hover:underline">
                    {changeDetail(c)}
                  </Link>
                </div>
              </div>
              <div className="shrink-0 text-sm pt-0.5">
                <UndoButton change={c} state={rowState} onUndo={() => void undo(c)} />
              </div>
            </li>
          );
        })}
      </ul>
      {changes.length > PREVIEW && (
        <button
          type="button"
          onClick={() => setAll((a) => !a)}
          className="mt-1 inline-flex h-10 items-center gap-1 rounded-xl px-2 -ml-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors cursor-pointer"
        >
          {all ? "Show fewer" : `See all ${changes.length}`}
          {!all && <Icon name="chevron-right" size="w-4 h-4" />}
        </button>
      )}
    </section>
  );
}
