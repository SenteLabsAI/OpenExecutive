"use client";

import CadenceSection, { FollowUpsCard } from "./CadenceSection";
import MemorySection from "./MemorySection";
import PulseHeader from "./PulseHeader";

// The Pulse page is the Executive's memory + heartbeat: what it knows
// (durable episodic memory) and the rhythm it runs on (recurring briefs,
// reflections, department check-ins, and internal scans). Both halves are
// built from data that already exists — episodic memory rows and the
// scheduled_actions queue grouped by `kind`.
//
// Layout: an at-a-glance header (stat strip + heartbeat heatmap) spans the
// full width, then Cadence ("Heartbeat") and Memory ("What it knows") sit
// side by side on wide screens and stack on smaller ones.

export default function PulsePage() {
  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 py-8 space-y-10">
      <PulseHeader />

      {/* `min-w-0` on each grid child: fr tracks default to min-width:auto, so
          a long unbreakable line (e.g. an activity summary) would otherwise
          force the track — and the whole page — wider than the viewport. */}
      <div className="grid gap-8 xl:grid-cols-[1.05fr_0.95fr]">
        <section className="min-w-0">
          <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle mb-4">
            Heartbeat — the rhythm it runs on
          </h2>
          <CadenceSection />
        </section>

        <section className="min-w-0 space-y-8">
          <div>
            <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-subtle mb-4">
              Memory — what it knows
            </h2>
            <MemorySection />
          </div>
          <FollowUpsCard />
        </section>
      </div>
    </div>
  );
}
