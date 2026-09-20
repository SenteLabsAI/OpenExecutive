"use client";

import { SettingsAdvancedDirectory, SettingsTabBody } from "@/components/settings/SettingsShell";
import { getTab } from "@/components/settings/registry";

export default function SettingsPage() {
  return (
    <>
      <SettingsTabBody tab={getTab("general")} />
      <SettingsAdvancedDirectory />
    </>
  );
}
