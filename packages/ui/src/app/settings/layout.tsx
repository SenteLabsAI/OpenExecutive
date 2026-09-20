"use client";

import type { ReactNode } from "react";

import { SettingsTabNav } from "@/components/settings/SettingsShell";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
        <p className="text-xs uppercase tracking-wide text-fg-muted">Settings</p>
        <div className="mt-3">
          <SettingsTabNav />
        </div>
        {children}
      </div>
    </main>
  );
}
