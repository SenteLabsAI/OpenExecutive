"use client";

import Link from "next/link";

import { useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";
import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import VoicePicker from "@/components/executive/VoicePicker";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// What it always does without asking: no switch, only Pause stops these.
const ALWAYS_DOES: { title: string; text: string }[] = [
  { title: "Morning brief and evening digest", text: "Sums up your day and what's waiting for you." },
  { title: "Nudges", text: "Reminds people about things they owe you." },
  { title: "Looks over your day", text: "Spots what's stuck or coming up and tells you." },
  { title: "Research and monitoring", text: "Follows the topics and sources you asked it to watch." },
];

// Settings → Your Executive: Pause, what it always does without asking, and
// the voice it answers in. Pause stops everything it does on its own,
// including Take the lead and booking meetings, which live under Delegate
// with what it sends as you. /settings/on-its-own lands here.
export default function ExecutiveSettingsPage() {
  return (
    <SettingsSubpage
      title="Your Executive"
      description="Pause it, what it always does without asking, and the voice it answers in."
    >
      <SettingsCard
        title="Pause everything"
        description="Stops everything it does on its own, Take the lead included, plus briefs, nudges and research. It still answers when someone messages it, and anything you tap Send or Approve on still goes."
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

      <p className="text-[15px] text-fg-muted">
        Take the lead, booking meetings and what it sends as you are under{" "}
        <Link href="/delegate/take-the-lead" className="font-medium text-accent underline-offset-2 hover:underline">
          Delegate
        </Link>
        .
      </p>

      <SettingsCard title="Voice" description="How it sounds when it answers you.">
        <VoicePicker variant="card" />
      </SettingsCard>
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
