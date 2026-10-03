"use client";

import SettingsSubpage from "@/components/settings/SettingsSubpage";
import WorkspaceCard from "@/components/settings/WorkspaceCard";

// Settings → Workspace: who Open Executive is for, and when and how it acts.
export default function WorkspaceSettingsPage() {
  return (
    <SettingsSubpage
      title="Workspace"
      description="Who Open Executive is for, the time zone your briefs run in, and whether it books meetings on its own."
    >
      <WorkspaceCard />
    </SettingsSubpage>
  );
}
