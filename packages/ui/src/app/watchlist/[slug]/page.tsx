"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  deleteWatchlistItem,
  getWatchlistItem,
  getWatchlistSignals,
  patchWatchlistItem,
  type WatchlistItem,
  type WatchlistSignal,
} from "@/lib/api";

/** Relative time for display, e.g. "3h ago", "just now", "never". */
function formatRelTime(iso: string | null): string {
  if (!iso) return "never";
  try {
    const diff = new Date(iso).getTime() - Date.now();
    // new Date("garbage") yields NaN rather than throwing; every comparison
    // below would be false and fall through to "NaNd ago".
    if (!Number.isFinite(diff)) return "—";
    // A timestamp slightly ahead of the browser clock (published_at within
    // the server's skew tolerance) reads as "just now", never a fabricated
    // past.
    // A value well ahead of the browser clock is reported as such rather
    // than dressed up as recent (the source controls published_at). The
    // tolerance absorbs ordinary browser/server clock drift, so our own
    // timestamps don't read as tampered with.
    if (diff > 300_000) return "in the future";
    if (diff > -60_000) return "just now";
    const abs = Math.abs(diff);
    if (abs < 3_600_000) return `${Math.round(abs / 60_000)}m ago`;
    if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)}h ago`;
    return `${Math.round(abs / 86_400_000)}d ago`;
  } catch {
    return "—";
  }
}

function outcomeStyle(outcome: string | null): string {
  if (outcome === "alerted") return "bg-emerald-500/20 text-emerald-300 border-emerald-500/30";
  if (outcome === "failed") return "bg-rose-500/20 text-rose-300 border-rose-500/30";
  if (outcome === null) return "bg-amber-500/20 text-amber-300 border-amber-500/30";
  // suppressed_* variants
  return "bg-zinc-500/20 text-zinc-400 border-zinc-500/30";
}

function outcomeLabel(outcome: string | null): string {
  if (outcome === null) return "pending";
  return outcome.replace(/^suppressed_/, "suppressed: ");
}

function SignalRow({ signal }: { signal: WatchlistSignal }) {
  return (
    <li className="border border-line rounded-lg bg-surface-elevated p-3">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex-1 min-w-0">
          <div className="text-sm text-fg truncate" title={signal.normalized_summary}>
            {signal.normalized_summary}
          </div>
          <div className="text-[11px] text-fg-muted mt-0.5">
            {signal.published_at
              ? `published ${formatRelTime(signal.published_at)} · seen ${formatRelTime(signal.captured_at)}`
              : formatRelTime(signal.captured_at)}
            {" "}· severity {signal.severity_hint}
          </div>
        </div>
        <span
          className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${outcomeStyle(signal.processed_outcome)}`}
        >
          {outcomeLabel(signal.processed_outcome)}
        </span>
      </div>
      <div className="flex items-center gap-3 text-[11px]">
        {signal.provenance_url && (
          <a
            href={signal.provenance_url}
            target="_blank"
            rel="noreferrer"
            className="text-indigo-300 hover:text-indigo-200 truncate max-w-xs"
          >
            source ↗
          </a>
        )}
        {signal.promoted_alert_id != null && (
          <span className="text-fg-subtle">alert #{signal.promoted_alert_id}</span>
        )}
      </div>
    </li>
  );
}

export default function WatchDetailPage() {
  const params = useParams<{ slug: string }>();
  const router = useRouter();
  // Next.js already decodes dynamic params; no second decode needed.
  const slug = params?.slug ?? "";

  const [item, setItem] = useState<WatchlistItem | null>(null);
  const [signals, setSignals] = useState<WatchlistSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [savingNotes, setSavingNotes] = useState(false);
  const [confirming, setConfirming] = useState(false);
  // Single in-flight slot per mutation type. Prevents the optimistic
  // toggle-revert race where two clicks land in the wrong final state.
  const [toggling, setToggling] = useState(false);
  const [modeChanging, setModeChanging] = useState(false);

  useEffect(() => {
    if (!slug) return;
    setLoading(true);
    Promise.all([getWatchlistItem(slug), getWatchlistSignals(slug, 50)])
      .then(([w, s]) => {
        setItem(w);
        setNotes(w.notes);
        setSignals(s);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [slug]);

  async function toggleEnabled() {
    if (!item || toggling) return;
    const next = !item.enabled;
    setToggling(true);
    setItem({ ...item, enabled: next });
    try {
      const updated = await patchWatchlistItem(slug, { enabled: next });
      setItem(updated);
    } catch (e) {
      setItem({ ...item, enabled: !next });
      setError(e instanceof Error ? e.message : "Toggle failed");
    } finally {
      setToggling(false);
    }
  }

  async function toggleMode() {
    if (!item || modeChanging) return;
    const nextMode = item.mode === "active" ? "dry_run" : "active";
    setModeChanging(true);
    try {
      const updated = await patchWatchlistItem(slug, { mode: nextMode });
      setItem(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Mode change failed");
    } finally {
      setModeChanging(false);
    }
  }

  async function saveNotes() {
    if (!item || notes === item.notes) return;
    setSavingNotes(true);
    try {
      const updated = await patchWatchlistItem(slug, { notes });
      setItem(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSavingNotes(false);
    }
  }

  async function remove() {
    try {
      await deleteWatchlistItem(slug);
      router.push("/watchlist");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
      setConfirming(false);
    }
  }

  if (loading) {
    return (
      <div className="flex flex-col h-full bg-surface p-6">
        <p className="text-fg-muted text-sm">Loading…</p>
      </div>
    );
  }
  if (error && !item) {
    return (
      <div className="flex flex-col h-full bg-surface p-6">
        <Link href="/watchlist" className="text-xs text-indigo-300 hover:text-indigo-200">
          ← Back
        </Link>
        <div className="mt-4 p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
          {error}
        </div>
      </div>
    );
  }
  if (!item) return null;

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6">
          <Link
            href="/watchlist"
            className="text-xs text-indigo-300 hover:text-indigo-200 inline-block mb-4"
          >
            ← Watch list
          </Link>

          <div className="flex items-baseline justify-between mb-4 gap-4">
            <div className="min-w-0">
              <h1 className="text-xl font-mono font-semibold text-fg truncate">{item.slug}</h1>
              <p className="text-sm text-fg-muted mt-0.5 truncate" title={item.target}>
                {item.signal_type} · {item.target}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={toggleEnabled}
                disabled={toggling}
                className={`px-3 py-1.5 text-xs rounded-lg border disabled:opacity-60 ${
                  item.enabled
                    ? "bg-indigo-500/20 text-indigo-300 border-indigo-500/30 hover:bg-indigo-500/30"
                    : "bg-zinc-500/20 text-zinc-400 border-zinc-500/30 hover:bg-zinc-500/30"
                }`}
              >
                {item.enabled ? "Enabled" : "Disabled"}
              </button>
              <button
                onClick={toggleMode}
                disabled={modeChanging}
                className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay text-fg-muted disabled:opacity-60"
              >
                mode: {item.mode}
              </button>
            </div>
          </div>

          {error && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm mb-4">
              {error}
            </div>
          )}

          <div className="rounded-xl border border-line bg-surface-elevated p-4 mb-4">
            <h2 className="text-xs uppercase tracking-wide text-fg-subtle mb-3">Configuration</h2>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
              <dt className="text-fg-muted">Cadence</dt>
              <dd className="text-fg">{item.cadence}</dd>
              <dt className="text-fg-muted">Severity range</dt>
              <dd className="text-fg">
                {item.severity_floor} → {item.severity_ceiling}
              </dd>
              <dt className="text-fg-muted">Specialist</dt>
              <dd className="text-fg">{item.route_to_specialist || "—"}</dd>
              <dt className="text-fg-muted">Fired count</dt>
              <dd className="text-fg">{item.fired_count}</dd>
              <dt className="text-fg-muted">Dismissed</dt>
              <dd className="text-fg">{item.dismiss_count}</dd>
              <dt className="text-fg-muted">Trust score</dt>
              <dd className="text-fg">{item.trust_score.toFixed(2)}</dd>
              <dt className="text-fg-muted">Last polled</dt>
              <dd className="text-fg">{formatRelTime(item.last_polled_at)}</dd>
              <dt className="text-fg-muted">Last fired</dt>
              <dd className="text-fg">{formatRelTime(item.last_fired_at)}</dd>
            </dl>
            <div className="mt-3">
              <div className="text-xs text-fg-muted mb-1">Trigger</div>
              <pre className="text-[11px] font-mono bg-surface-input/40 border border-line rounded p-2 overflow-x-auto">
                {JSON.stringify(item.trigger_json, null, 2)}
              </pre>
            </div>
          </div>

          <div className="rounded-xl border border-line bg-surface-elevated p-4 mb-4">
            <h2 className="text-xs uppercase tracking-wide text-fg-subtle mb-2">Notes</h2>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              onBlur={saveNotes}
              rows={3}
              maxLength={500}
              placeholder="Why are we watching this?"
              className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
            />
            {savingNotes && (
              <p className="text-[10px] text-fg-subtle mt-1">Saving…</p>
            )}
          </div>

          <div className="rounded-xl border border-line bg-surface-elevated p-4 mb-4">
            <h2 className="text-xs uppercase tracking-wide text-fg-subtle mb-3">
              Recent signals ({signals.length})
            </h2>
            {signals.length === 0 ? (
              <p className="text-xs text-fg-muted">
                No signals yet. The next poll runs on cadence: {item.cadence}.
              </p>
            ) : (
              <ul className="space-y-2">
                {signals.map((s) => (
                  <SignalRow key={s.id} signal={s} />
                ))}
              </ul>
            )}
          </div>

          <div className="rounded-xl border border-rose-500/30 bg-rose-500/5 p-4">
            <h2 className="text-xs uppercase tracking-wide text-rose-300 mb-2">Danger zone</h2>
            {confirming ? (
              <div className="flex items-center gap-2">
                <span className="text-xs text-fg-muted">
                  Delete this monitor? Historical signals stay in the audit log.
                </span>
                <button
                  onClick={remove}
                  className="px-3 py-1.5 text-xs rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-medium"
                >
                  Delete
                </button>
                <button
                  onClick={() => setConfirming(false)}
                  className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirming(true)}
                className="px-3 py-1.5 text-xs rounded-lg border border-rose-500/30 text-rose-300 hover:bg-rose-500/10"
              >
                Delete monitor
              </button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
