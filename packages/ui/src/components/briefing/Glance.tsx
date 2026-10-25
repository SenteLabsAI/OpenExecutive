"use client";

import { useEffect, useState } from "react";

import { getHandledReplies, listCustomWorkflows, listWatchlist, listWorkflowRuns } from "@/lib/api";
import { handledThisWeek, scheduledWorkflows, watching } from "@/lib/glance";

// The counts behind Home's "At a glance" tiles that /today doesn't carry:
// what Act as me handled this week, your scheduled workflows, and the
// watchlist. Each is null when it can't be read (not allowed for this viewer,
// or the call failed), and its tile stays hidden.
export interface Glance {
  actAsMe: number | null;
  workflows: { scheduled: number; runsThisWeek: number } | null;
  watching: { watched: number; firedThisWeek: number } | null;
}

const NONE: Glance = { actAsMe: null, workflows: null, watching: null };

export function useGlance(enabled: boolean): Glance {
  const [glance, setGlance] = useState<Glance>(NONE);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let cancelled = false;
    const now = new Date();
    const set = (part: Partial<Glance>) => {
      if (!cancelled) setGlance((g) => ({ ...g, ...part }));
    };
    getHandledReplies(controller.signal)
      .then((replies) => set({ actAsMe: replies ? handledThisWeek(replies, now) : null }))
      .catch(() => {});
    Promise.all([listCustomWorkflows(), listWorkflowRuns()])
      .then(([defs, runs]) => set({ workflows: scheduledWorkflows(defs, runs, now) }))
      .catch(() => {});
    listWatchlist({ enabledOnly: true })
      .then((items) => set({ watching: watching(items, now) }))
      .catch(() => {});
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [enabled]);

  return glance;
}
