"use client";

import AboutCard from "@/components/settings/AboutCard";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → About: the version this install runs.
export default function AboutSettingsPage() {
  return (
    <SettingsSubpage
      title="About"
      description="The version this install is running, and whether a newer release is out."
    >
      <SettingsCard>
        <AboutCard />
      </SettingsCard>
    </SettingsSubpage>
  );
}
