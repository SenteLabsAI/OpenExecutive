"use client";

import Icon from "@/components/Icon";
import { formatPausedAt, useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";

// Full-width strip at the top of the main column while the Executive is
// paused, so the state is impossible to miss on any page — including mobile,
// where the sidebar switch sits behind the drawer.
export default function PausedBanner() {
  const { status, busy, resume } = useExecutiveStatus();
  if (!status?.paused) return null;

  const since = formatPausedAt(status.paused_at);
  return (
    <div
      role="status"
      className="flex items-center gap-3 px-4 sm:px-6 py-2 border-b border-amber-500/30 bg-amber-500/10 text-amber-200 flex-shrink-0"
    >
      <Icon name="pause" size="w-4 h-4" />
      <p className="flex-1 min-w-0 text-xs leading-snug">
        <span className="font-medium">Executive paused{since && ` since ${since}`}</span>
        <span className="hidden sm:inline">
          {" "}
          — briefs, nudges, monitoring, inbox and workflow timers are on hold. Chat still works.
        </span>
        {status.held_actions > 0 && (
          <span className="text-amber-300/80"> · {status.held_actions} waiting</span>
        )}
      </p>
      <button
        type="button"
        onClick={() => void resume()}
        disabled={busy}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-amber-500/20 hover:bg-amber-500/30 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
      >
        <Icon name="play" size="w-3.5 h-3.5" />
        {busy ? "Resuming…" : "Resume"}
      </button>
    </div>
  );
}
