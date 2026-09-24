"use client";

import { useState } from "react";

import Icon from "@/components/Icon";
import { formatPausedAt, useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";

// What a pause does and does not stop — shown before pausing so the
// principal knows chat stays live.
export const PAUSE_SCOPE =
  "Pausing holds briefs, nudges, monitoring, inbox processing and workflow timers. Chat and direct messages keep working. Nothing is lost — held work runs when you resume.";

function heldLabel(n: number): string {
  if (n <= 0) return "Nothing is waiting yet.";
  return `${n} held action${n === 1 ? "" : "s"} will run when you resume.`;
}

/**
 * Pause / resume the Executive's autonomous work.
 *
 * - `sidebar`: a one-line status row in the shared sidebar footer that
 *   expands into the pause/resume panel.
 * - `card`: the always-expanded panel, for the Settings page.
 */
export default function ExecutiveRunSwitch({ variant }: { variant: "sidebar" | "card" }) {
  const { status, unknown, busy, error, pause, resume } = useExecutiveStatus();
  const [expanded, setExpanded] = useState(false);
  const [reason, setReason] = useState("");

  if (!status) return null;
  if (unknown) {
    // The last status read failed: say so rather than show a stale state,
    // and offer no action whose effect we can't confirm.
    return (
      <div
        className={variant === "card" ? "rounded-xl border border-line bg-surface-elevated p-4" : "pb-1"}
        title="Couldn't reach the backend — retrying"
      >
        <div className="flex items-center gap-2.5 px-3 py-2 text-sm text-fg-muted">
          <span className="inline-block w-2 h-2 rounded-full flex-shrink-0 bg-fg-subtle" aria-hidden="true" />
          <span className="truncate">Executive status unknown</span>
        </div>
      </div>
    );
  }
  const paused = status.paused;
  const open = variant === "card" || expanded;

  // Collapse only on success so a failure's error stays visible.
  const onPause = async () => {
    if (await pause(reason)) {
      setReason("");
      setExpanded(false);
    }
  };
  const onResume = async () => {
    if (await resume()) setExpanded(false);
  };

  const dot = (
    <span
      className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${
        paused ? "bg-amber-400" : "bg-emerald-400"
      }`}
      aria-hidden="true"
    />
  );
  const label = paused ? "Executive paused" : "Executive running";

  const panel = (
    <div className="space-y-2.5">
      {paused ? (
        <>
          <p className="text-xs text-fg-muted leading-relaxed">
            Paused
            {status.paused_at && <> since {formatPausedAt(status.paused_at)}</>}
            {status.paused_by && <> by {status.paused_by}</>}.
            {status.reason && (
              <>
                {" "}
                <span className="text-fg">&ldquo;{status.reason}&rdquo;</span>
              </>
            )}
          </p>
          <p className="text-xs text-amber-300">{heldLabel(status.held_actions)}</p>
          {!status.can_resume ? (
            <p className="text-xs text-fg-muted">Only the principal can resume the Executive.</p>
          ) : (
          <button
            type="button"
            onClick={onResume}
            disabled={busy}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-500/15 text-emerald-200 hover:bg-emerald-500/25 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Icon name="play" size="w-3.5 h-3.5" />
            {busy ? "Resuming…" : "Resume Executive"}
          </button>
          )}
        </>
      ) : (
        <>
          <p className="text-xs text-fg-muted leading-relaxed">{PAUSE_SCOPE}</p>
          <input
            type="text"
            value={reason}
            maxLength={200}
            onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !busy) void onPause();
            }}
            placeholder="Reason (optional)"
            aria-label="Reason for pausing (optional)"
            className="w-full px-2.5 py-1.5 rounded-lg text-xs bg-surface border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:border-line-strong"
          />
          <button
            type="button"
            onClick={onPause}
            disabled={busy}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-500/15 text-amber-200 hover:bg-amber-500/25 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Icon name="pause" size="w-3.5 h-3.5" />
            {busy ? "Pausing…" : "Pause Executive"}
          </button>
        </>
      )}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );

  if (variant === "card") {
    return (
      <section className="rounded-xl border border-line bg-surface-elevated p-4">
        <div className="flex items-center gap-2.5">
          {dot}
          <h2 className="text-sm font-medium text-fg">{label}</h2>
        </div>
        <div className="mt-3 max-w-md">{panel}</div>
      </section>
    );
  }

  return (
    <div className="pb-1">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={open}
        title={paused ? "Autonomous work is on hold — click to resume" : "Pause the Executive's autonomous work"}
        className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors cursor-pointer ${
          paused
            ? "text-amber-200 bg-amber-500/10 hover:bg-amber-500/15"
            : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
        }`}
      >
        {dot}
        <span className="flex-1 text-left truncate">{label}</span>
        <Icon name={paused ? "play" : "pause"} size="w-4 h-4" />
      </button>
      {open && <div className="px-3 pt-2 pb-1">{panel}</div>}
    </div>
  );
}
