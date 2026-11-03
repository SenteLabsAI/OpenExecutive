"use client";

import Link from "next/link";

import { CompanyRetentionCard, KeepTrackCard, ShareWorkStyleCard } from "@/components/settings/HistorySettings";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → About you: the switches for what the Executive keeps about the
// signed-in person, which only they see. What it has learned about them is on
// the Memory page (About you); here: whether they share their work style
// with the team, their own "Keep track of what happens" switch, and how long
// notes last for everyone.
export default function MemorySettingsPage() {
  return (
    <SettingsSubpage
      title="What the Executive keeps about you"
      description="Whether it keeps notes of what you tell it, and what you share with the team. Each person sees only their own."
    >
      <div className="space-y-4">
        <SettingsCard
          title="What it knows about you"
          description="What it has learned from talking with you is on the Memory page, where you can remove any line."
        >
          <Link href="/memories" className="inline-flex min-h-touch items-center text-[15px] font-semibold text-accent hover:underline">
            Open Memory
          </Link>
        </SettingsCard>
        <ShareWorkStyleCard />
        <KeepTrackCard />
        <CompanyRetentionCard />
      </div>
    </SettingsSubpage>
  );
}
