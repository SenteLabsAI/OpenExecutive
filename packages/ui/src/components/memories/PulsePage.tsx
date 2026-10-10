"use client";

import { useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { isMemoryView, pulseViewFor, type PulseView } from "@/lib/pulseView";
import RhythmSection, { FollowUpsCard, RecentActivity } from "./CadenceSection";
import MemoryList from "./MemorySection";
import { HeartbeatCard, usePulseData } from "./PulseHeader";
import { ComingUp, DoneRecently, FocusedScreen, MemoryTiles } from "./PulseOverview";

// The Pulse page is the Executive's memory + heartbeat: what it knows
// (durable episodic memory) and the rhythm it runs on (recurring briefs,
// reflections, department check-ins, and internal scans). Both halves are
// built from data that already exists — episodic memory rows and the
// scheduled_actions queue grouped by `kind`.
//
// Layout: one overview with no tabs (the heartbeat card with its numbers,
// a tile per kind of memory, then Coming up beside Done recently), and a
// focused screen behind each link, picked by `?tab=` so the older tab links
// still land (lib/pulseView.ts). What it has learned about the signed-in
// person is theirs alone, so it lives in Settings → About you, not here.

const SCREENS: Record<Exclude<PulseView, "overview">, { title: string; description: string }> = {
  schedule: {
    title: "Schedule",
    description: "Everything the Executive will do on its own, soonest first.",
  },
  activity: {
    title: "Activity",
    description: "What the Executive did on its own, newest first.",
  },
  decisions: {
    title: "Decisions",
    description: "Decisions picked up from your chats. Future answers build on them.",
  },
  initiatives: {
    title: "Projects",
    description: "The projects it's tracking, from planned to completed.",
  },
  advice: {
    title: "Advice",
    description: "Advice its specialists gave, by topic.",
  },
  corrections: {
    title: "Facts you taught it",
    description: "Facts and corrections you asked it to keep.",
  },
  history: {
    title: "Your notes",
    description: "What you said, in chat and in replies you approved. Only you see these.",
  },
};

export default function PulsePage() {
  const router = useRouter();
  // `/memories?tab=corrections` (the chat chip after remember_fact),
  // `?tab=history` (Settings) and the other screen names open that screen.
  const wanted = useSearchParams().get("tab");
  const view = pulseViewFor(wanted);
  const top = useRef<HTMLDivElement>(null);

  // The People tab moved to Settings → About you; old links follow it there.
  useEffect(() => {
    if (wanted === "people") router.replace("/settings/memory");
  }, [router, wanted]);

  // Moving between the overview and a screen starts at the top.
  useEffect(() => {
    top.current?.scrollIntoView({ block: "start" });
  }, [view]);

  return (
    <div ref={top} className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-8 space-y-6">
      {view === "overview" ? (
        <Overview />
      ) : (
        <FocusedScreen {...SCREENS[view]}>
          {view === "schedule" && (
            <div className="space-y-5">
              <RhythmSection />
              <FollowUpsCard />
            </div>
          )}
          {view === "activity" && <RecentActivity />}
          {isMemoryView(view) && <MemoryList view={view} />}
        </FocusedScreen>
      )}
    </div>
  );
}

/** Loaded each time it's shown, so counts reflect edits made on a list. */
function Overview() {
  const pulse = usePulseData();
  return (
    <>
      <header>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Pulse</h1>
        <p className="text-[15px] text-fg-muted mt-1.5 max-w-2xl">
          What your Executive does on its own, and what it remembers.
        </p>
      </header>
      <HeartbeatCard pulse={pulse} />
      <MemoryTiles pulse={pulse} />
      <div className="grid gap-6 md:grid-cols-2">
        <ComingUp pulse={pulse} />
        <DoneRecently />
      </div>
    </>
  );
}
