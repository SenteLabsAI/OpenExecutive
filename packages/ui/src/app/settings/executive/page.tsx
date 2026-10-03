"use client";

import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import VoicePicker from "@/components/executive/VoicePicker";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → Your Executive: whether it is doing its own work or holding it,
// and the voice it answers in — the one place the voice is chosen (the Agent
// Council's advanced view keeps the persona editor for custom voices).
export default function ExecutiveSettingsPage() {
  return (
    <SettingsSubpage
      title="Your Executive"
      description="Whether the Executive is doing its own work — briefs, nudges, monitoring, inbox, workflow timers — or holding it, and the voice it answers in."
    >
      <SettingsCard>
        <ExecutiveRunSwitch />
      </SettingsCard>
      <SettingsCard title="Voice" description="How it sounds when it answers you.">
        <VoicePicker variant="card" />
      </SettingsCard>
    </SettingsSubpage>
  );
}
