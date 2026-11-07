import type { ReactNode } from "react";

import SettingsShell from "@/components/settings/SettingsShell";

// The Settings menu beside every Settings page (from `lg` up).
export default function Layout({ children }: { children: ReactNode }) {
  return <SettingsShell>{children}</SettingsShell>;
}
