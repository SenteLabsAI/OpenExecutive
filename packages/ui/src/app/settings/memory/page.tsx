"use client";

import { CompanyRetentionCard } from "@/components/settings/HistorySettings";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → Memory: how long the private notes Always in the loop keeps last.
export default function MemorySettingsPage() {
  return (
    <SettingsSubpage
      title="Memory"
      description="The Executive keeps private notes of what people said in replies they approved and sent, so it can remind them later. Each person sees only their own."
    >
      <CompanyRetentionCard />
    </SettingsSubpage>
  );
}
