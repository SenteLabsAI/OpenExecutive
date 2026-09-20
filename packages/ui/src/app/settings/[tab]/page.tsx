"use client";

import { useParams } from "next/navigation";
import { SettingsAdvancedDirectory, SettingsTabBody } from "@/components/settings/SettingsShell";
import { getTab } from "@/components/settings/registry";

export default function SettingsTabPage() {
  const params = useParams();
  const tab = getTab(String(params?.tab ?? "general"));
  return (
    <>
      <SettingsTabBody tab={tab} />
      {tab.id === "general" ? <SettingsAdvancedDirectory /> : null}
    </>
  );
}
