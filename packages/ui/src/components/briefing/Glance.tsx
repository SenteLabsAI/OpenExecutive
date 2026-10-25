"use client";

import { useEffect, useState } from "react";

import { getHandledReplies, listCustomWorkflows, listWatchlist, listWorkflowRuns } from "@/lib/api";
import { handledThisWeek, scheduledNames, scheduledWorkflows, watching } from "@/lib/glance";

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
    // Each scheduled workflow's own runs: the unfiltered list is the latest
    // 100 of every workflow, which the system's own runs can fill.
    listCustomWorkflows()
      .then(async (defs) => {
        const names = scheduledNames(defs);
        const runs = (await Promise.all(names.map((name) => listWorkflowRuns(name)))).flat();
        set({ workflows: scheduledWorkflows(defs, runs, now) });
      })
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
