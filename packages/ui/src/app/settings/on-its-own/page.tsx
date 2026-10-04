"use client";

import { useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";
import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import HandleItCard from "@/components/settings/HandleItCard";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import TakeTheLeadCard from "@/components/settings/TakeTheLeadCard";
import { MeetingAutonomySwitch } from "@/components/settings/WorkspaceCard";

// What it always does without asking: no switch, only Pause stops these.
const ALWAYS_DOES: { title: string; text: string }[] = [
  { title: "Morning brief and evening digest", text: "Sums up your day and what's waiting for you." },
  { title: "Nudges", text: "Reminds people about things they owe you." },
  { title: "Looks over your day", text: "Spots what's stuck or coming up and tells you." },
  { title: "Research and monitoring", text: "Follows the topics and sources you asked it to watch." },
];

// Settings → On its own: everything the Executive does without asking first,
// in one place. Pause at the top stops all of it; then what it always does,
// what it does as itself (Take the lead, booking meetings), and what it does
// as you, from your own mailbox (Handle it for me, one dial up to Take the
// lead).
export default function OnItsOwnSettingsPage() {
  return (
    <SettingsSubpage
      title="On its own"
      description="What the Executive does without asking you first, and how much."
    >
      <SettingsCard
        title="Pause everything"
        description="Stops everything on this page, plus briefs, nudges and research. It still answers when someone messages it, and anything you tap Send or Approve on still goes."
      >
        <ExecutiveRunSwitch />
      </SettingsCard>

      <PausedNote />

      <SettingsCard title="Always does on its own" description="No switch for these. Pause stops them too.">
        <ul className="flex flex-col gap-3">
          {ALWAYS_DOES.map((item) => (
            <li key={item.title} className="text-[15px] leading-snug">
              <span className="font-semibold text-fg">{item.title}.</span>{" "}
              <span className="text-fg-muted">{item.text}</span>
            </li>
          ))}
        </ul>
      </SettingsCard>

      <section aria-labelledby="on-its-own-exec" className="space-y-3">
        <div>
          <h2 id="on-its-own-exec" className="text-lg font-semibold text-fg">As the Executive</h2>
          <p className="mt-1 text-[15px] text-fg-muted">In its own name. People can see it&apos;s the Executive.</p>
        </div>
        <TakeTheLeadCard />
        <MeetingAutonomySwitch />
      </section>

      <section aria-labelledby="on-its-own-you" className="space-y-3">
        <div>
          <h2 id="on-its-own-you" className="text-lg font-semibold text-fg">As you</h2>
          <p className="mt-1 text-[15px] text-fg-muted">From your own mailbox, in your name.</p>
        </div>
        <HandleItCard />
      </section>
    </SettingsSubpage>
  );
}

// While paused, say so above the switches, so nothing below reads as running.
function PausedNote() {
  const { status } = useExecutiveStatus();
  if (!status?.paused) return null;
  return (
    <p
      role="status"
      className="rounded-xl border border-amber-400/50 bg-amber-400/10 px-4 py-3 text-[15px] font-medium text-fg"
    >
      Paused. Nothing on this page runs until you resume, whatever its switch says.
    </p>
  );
}
