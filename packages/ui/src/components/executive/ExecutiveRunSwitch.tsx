"use client";

import { useState } from "react";

import Icon from "@/components/Icon";
import { formatPausedAt, useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";
import Button from "@/components/ui/Button";

// What a pause does and does not stop — shown before pausing so the
// principal knows chat stays live.
export const PAUSE_SCOPE =
  "Pausing holds briefs, nudges, monitoring, inbox processing and workflow timers. Chat and direct messages keep working. Nothing is lost — held work runs when you resume.";

function heldLabel(n: number): string {
  if (n <= 0) return "Nothing is waiting yet.";
  return `${n} held action${n === 1 ? "" : "s"} will run when you resume.`;
}

/**
 * Pause / resume the Executive's autonomous work: the body of the run card
 * on Settings → Your Executive. A status row with one button — Pause… opens
 * the pause form (scope and an optional reason); while paused, who paused it
 * and the held count stay in view with Resume as the button.
 */
export default function ExecutiveRunSwitch() {
  const { status, unknown, busy, error, pause, resume } = useExecutiveStatus();
  const [expanded, setExpanded] = useState(false);
  const [reason, setReason] = useState("");

  if (!status) return <p className="text-[15px] text-fg-muted">Loading…</p>;
  if (unknown) {
    // The last status read failed: say so rather than show a stale state,
    // and offer no action whose effect we can't confirm.
    return (
      <div
        className="flex items-center gap-2.5 text-[15px] text-fg-muted"
        title="Couldn't reach the backend — retrying"
      >
        <span className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0 bg-fg-subtle" aria-hidden="true" />
        <span>Executive status unknown</span>
      </div>
    );
  }
  const paused = status.paused;

  // Collapse only on success so a failure's error stays visible.
  const onPause = async () => {
    if (await pause(reason)) {
      setReason("");
      setExpanded(false);
    }
  };

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5 text-base font-semibold text-fg">
          <span
            className={`inline-block w-2.5 h-2.5 rounded-full flex-shrink-0 ${
              paused ? "bg-amber-400" : "bg-emerald-500"
            }`}
            aria-hidden="true"
          />
          <span>{paused ? "Paused" : "Running"}</span>
        </div>
        {paused ? (
          status.can_resume && (
            <Button variant="primary" onClick={() => void resume()} disabled={busy}>
              <Icon name="play" size="w-4 h-4" />
              {busy ? "Resuming…" : "Resume"}
            </Button>
          )
        ) : (
          !expanded && (
            <Button
              onClick={() => setExpanded(true)}
              aria-expanded={false}
              aria-controls="executive-pause-panel"
            >
              <Icon name="pause" size="w-4 h-4" />
              Pause…
            </Button>
          )
        )}
      </div>

      {paused ? (
        <div className="mt-3 space-y-1.5">
          <p className="text-sm text-fg-muted leading-relaxed">
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
          <p className="text-sm text-amber-500">{heldLabel(status.held_actions)}</p>
          {!status.can_resume && (
            <p className="text-sm text-fg-muted">Only the principal can resume the Executive.</p>
          )}
        </div>
      ) : expanded ? (
        <div id="executive-pause-panel" className="mt-4 space-y-3">
          <p className="text-sm text-fg-muted leading-relaxed">{PAUSE_SCOPE}</p>
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
            className="w-full h-11 px-3.5 rounded-xl text-[15px] bg-surface border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:border-line-strong"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={() => void onPause()} disabled={busy}>
              <Icon name="pause" size="w-4 h-4" />
              {busy ? "Pausing…" : "Pause Executive"}
            </Button>
            <Button variant="ghost" onClick={() => setExpanded(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <p className="mt-2 text-sm text-fg-muted leading-relaxed">
          Doing its own work — briefs, nudges, monitoring, inbox and workflow timers.
        </p>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </div>
  );
}
