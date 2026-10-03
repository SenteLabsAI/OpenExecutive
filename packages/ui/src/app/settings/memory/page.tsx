"use client";

import { CompanyRetentionCard, KeepTrackCard } from "@/components/settings/HistorySettings";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → Memory: Always in the loop. Each person's own "Keep track of what
// happens" switch, and how long notes last for everyone.
export default function MemorySettingsPage() {
  return (
    <SettingsSubpage
      title="Memory"
      description="The Executive can keep private notes of what you tell it in chat and in replies you send, so it can remind you later. Each person sees only their own."
    >
      <div className="space-y-4">
        <KeepTrackCard />
        <CompanyRetentionCard />
      </div>
    </SettingsSubpage>
  );
}
