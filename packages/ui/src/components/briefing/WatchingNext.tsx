"use client";

import { useState } from "react";

import { stopWatching, type WatchingItem } from "@/lib/api";
import { watchingLine } from "@/lib/watchingNext";

// Watching next on Home, under Done for you: what the Executive is keeping
// an eye on for you and when it acts next (GET /today `watching`): the
// follow-ups it will send, then your standing jobs. Stop ends that one thing
// only (POST /today/watching/stop). What it left for you stays in Needs you.

export default function WatchingNext({ items }: { items: WatchingItem[] }) {
  const [stopped, setStopped] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const shown = items.filter((item) => !stopped.has(item.key));
  if (shown.length === 0) return null;

  const stop = async (item: WatchingItem) => {
    setBusy(item.key);
    setError(null);
    try {
      await stopWatching(item.key);
      setStopped((prev) => new Set(prev).add(item.key));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't stop it.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-labelledby="sec-watching-next" className="mb-8">
      <h2 id="sec-watching-next" className="mb-3 text-lg font-semibold text-fg">
        Watching next
      </h2>
      <ul className="rounded-2xl border border-line bg-surface-elevated px-4 sm:px-5">
        {shown.map((item) => {
          const line = watchingLine(item);
          return (
            <li key={item.key} className="flex items-center gap-3 border-t border-line py-3 first:border-t-0">
              <div className="min-w-0 flex-1">
                <p className="text-[15px] text-fg break-words">{item.title}</p>
                {line && <p className="text-sm text-fg-muted break-words">{line}</p>}
              </div>
              <button
                type="button"
                disabled={busy === item.key}
                onClick={() => void stop(item)}
                className="flex min-h-touch flex-none items-center text-sm font-semibold text-accent hover:underline disabled:opacity-60"
                aria-label={`Stop: ${item.title}`}
              >
                Stop
              </button>
            </li>
          );
        })}
      </ul>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </section>
  );
}
