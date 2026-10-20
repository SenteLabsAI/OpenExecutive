"use client";

import AboutYouCard from "@/components/settings/AboutYouCard";
import { CompanyRetentionCard, KeepTrackCard, ShareWorkStyleCard } from "@/components/settings/HistorySettings";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → About you: everything the Executive keeps about the signed-in
// person, which only they see. What peer memory has learned about them
// (their profile and notes), whether they share their work style with the
// team, then Always in the loop: their own "Keep track
// of what happens" switch, and how long notes last for everyone.
export default function MemorySettingsPage() {
  return (
    <SettingsSubpage
      title="What the Executive knows about you"
      description="What it has learned from talking with you, and the private notes it keeps of what you tell it. Each person sees only their own."
    >
      <div className="space-y-4">
        <AboutYouCard />
        <ShareWorkStyleCard />
        <KeepTrackCard />
        <CompanyRetentionCard />
      </div>
    </SettingsSubpage>
  );
}
