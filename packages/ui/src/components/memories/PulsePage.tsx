"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { readLastVisit, writeLastVisit } from "@/lib/memoryChanges";
import { isMemoryView, pulseViewFor, type PulseView } from "@/lib/pulseView";
import RhythmSection, { FollowUpsCard, RecentActivity } from "./CadenceSection";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import KnowsOverview from "./KnowsOverview";
import MemoryList from "./MemorySection";
import { HeartbeatCard, usePulseData } from "./PulseHeader";
import { ComingUp, DoneForYou, FocusedScreen } from "./PulseOverview";
import { ValueScreen } from "./ValueSection";
import HeldBack from "@/components/briefing/HeldBack";
import { getHeldBack, type HeldBackItem } from "@/lib/api";

// The Memory page (it was Pulse): what the Executive knows and what it does.
// Two tabs. What it knows: About you, About the company, What you taught it,
// What it worked out, and Learning (KnowsOverview). What it does: the
// heartbeat card with its numbers, then Coming up beside Done for you. Each
// links to a focused screen picked by `?tab=`, so older links still land
// (lib/pulseView.ts). What it learned about the signed-in person is theirs
// alone; its switches stay in Settings → About you.

const SCREENS: Record<Exclude<PulseView, "overview" | "does">, { title: string; description: string }> = {
  value: {
    title: "Done for you",
    description: "What the Executive did for you, counted from the record.",
  },
  schedule: {
    title: "Schedule",
    description: "Everything the Executive will do on its own, soonest first.",
  },
  activity: {
    title: "Activity",
    description: "What the Executive did on its own, newest first.",
  },
  worked_out: {
    title: "What it worked out",
    description: "Figures from its own work, with where they came from and when. Only you see these.",
  },
  initiatives: {
    title: "Projects",
    description: "The projects it's tracking, from planned to completed.",
  },
  advice: {
    title: "Older advice",
    description: "Advice its specialists gave before it kept figures instead.",
  },
  corrections: {
    title: "What you taught it",
    description: "Facts, corrections and decisions you asked it to keep.",
  },
  history: {
    title: "Your notes",
    description: "What you said, in chat and in replies you approved. Only you see these.",
  },
  reflection: {
    title: "Learning",
    description: "I learn from every chat. These are the lessons I use, in your words.",
  },
};

export default function PulsePage() {
  const router = useRouter();
  // `/memories?tab=corrections` (the chat chip after remember_fact),
  // `?tab=history` (Settings) and the other screen names open that screen.
  const wanted = useSearchParams().get("tab");
  const view = pulseViewFor(wanted);
  const top = useRef<HTMLDivElement>(null);
  // The visit before this one, for "New since your last visit"; this visit
  // becomes the next one's start (and clears the dot on Memory).
  const [lastVisit] = useState<string | null>(() => (typeof window === "undefined" ? null : readLastVisit()));
  useEffect(() => {
    writeLastVisit(new Date().toISOString());
  }, []);

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
      {view === "overview" || view === "does" ? (
        <Overview view={view} lastVisit={lastVisit} />
      ) : (
        <FocusedScreen
          {...SCREENS[view]}
          backHref={view === "schedule" || view === "activity" || view === "value" ? "/memories?tab=does" : "/memories"}
        >
          {view === "schedule" && (
            <div className="space-y-5">
              <RhythmSection />
              <FollowUpsCard />
            </div>
          )}
          {view === "activity" && <RecentActivity />}
          {view === "value" && <ValueScreen />}
          {isMemoryView(view) && <MemoryList view={view} />}
        </FocusedScreen>
      )}
    </div>
  );
}

type Tab = "overview" | "does";

/** Loaded each time it's shown, so counts reflect edits made on a list. */
function Overview({ view, lastVisit }: { view: Tab; lastVisit: string | null }) {
  const router = useRouter();
  const pulse = usePulseData();
  const idBase = useId();
  return (
    <>
      <header>
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Memory</h1>
        <p className="text-[15px] text-fg-muted mt-1.5 max-w-2xl">
          What your Executive knows and where it came from, and what it does on its own.
        </p>
      </header>
      <SectionTabs<Tab>
        tabs={[
          { id: "overview", label: "What it knows" },
          { id: "does", label: "What it does" },
        ]}
        active={view}
        onChange={(id) => router.replace(id === "does" ? "/memories?tab=does" : "/memories")}
        label="Memory"
        idBase={idBase}
      />
      <div {...sectionPanelProps(idBase, view)} className="space-y-6">
        {view === "overview" ? (
          <KnowsOverview pulse={pulse} lastVisit={lastVisit} />
        ) : (
          <>
            <HeartbeatCard pulse={pulse} />
            <LeftOutOfBrief />
            <div className="grid gap-6 md:grid-cols-2">
              <ComingUp pulse={pulse} />
              <DoneForYou />
            </div>
          </>
        )}
      </div>
    </>
  );
}

/** "Left out of your brief": the lines grounding held back from the briefs
 *  in the last day (the brief's note points here). Nothing when none were. */
function LeftOutOfBrief() {
  const [items, setItems] = useState<HeldBackItem[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    getHeldBack(controller.signal)
      .then(setItems)
      .catch(() => { /* nothing left out is better than a broken block */ });
    return () => controller.abort();
  }, []);
  if (items.length === 0) return null;
  return <HeldBack items={items} />;
}
