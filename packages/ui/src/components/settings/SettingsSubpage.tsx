"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import Icon from "@/components/Icon";
import { useSettingsShell } from "@/components/settings/SettingsShell";
import { usePageCrumb } from "@/components/shell/PageCrumb";

// The frame of each page the Settings hub opens: a way back to the hub, a
// big title, an optional line under it, and the page's cards.
export default function SettingsSubpage({
  title,
  description,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
}) {
  // Beside the Settings menu (SettingsShell, from `lg` up), the menu is the
  // way back, and the breadcrumb names the page as the menu does.
  const shell = useSettingsShell() !== null;
  usePageCrumb(shell && typeof title === "string" ? title : null);
  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className={`max-w-3xl mx-auto px-4 sm:px-6 pt-6 sm:pt-8 pb-16 ${shell ? "lg:mx-0 lg:px-10" : ""}`}>
        <Link
          href="/settings"
          className={`-ml-2 inline-flex min-h-touch items-center gap-1.5 rounded-lg px-2 text-[15px] text-fg-muted hover:text-fg hover:bg-surface-overlay transition-colors ${shell ? "lg:hidden" : ""}`}
        >
          <Icon name="arrow-left" size="w-4 h-4" />
          Settings
        </Link>
        <h1 className="mt-2 text-2xl sm:text-3xl font-bold tracking-tight text-fg">{title}</h1>
        {description && (
          <p className="mt-2 text-[15px] text-fg-muted leading-relaxed">{description}</p>
        )}
        <div className="mt-6 space-y-5">{children}</div>
      </div>
    </main>
  );
}
