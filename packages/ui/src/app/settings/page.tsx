"use client";

import Link from "next/link";

import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import Icon from "@/components/Icon";
import WorkspaceCard from "@/components/settings/WorkspaceCard";
import { ADVANCED_ITEMS } from "@/components/shell/navConfig";

// Settings hub — home for admin / power-user tools that were pulled out
// of the primary nav to keep day-to-day navigation focused. Each tool is
// a full route; this page is just the directory that points to them.
export default function SettingsPage() {
  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
        <h1 className="text-xl font-semibold text-fg">Settings &amp; advanced</h1>
        <p className="mt-1 text-sm text-fg-muted">
          Configuration, diagnostics, and power-user tools. These sit outside the
          day-to-day workspace nav.
        </p>

        <div className="mt-6">
          <ExecutiveRunSwitch variant="card" />
        </div>

        <div className="mt-3">
          <WorkspaceCard />
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          {ADVANCED_ITEMS.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="group rounded-xl border border-line bg-surface-elevated p-4 hover:border-line-strong hover:bg-surface-overlay transition-colors"
            >
              <div className="flex items-center gap-2.5">
                <span className="text-fg-muted group-hover:text-fg transition-colors">
                  <Icon name={item.icon} size="w-5 h-5" />
                </span>
                <h2 className="text-sm font-medium text-fg">{item.label}</h2>
              </div>
              <p className="mt-2 text-xs text-fg-muted leading-relaxed">
                {item.description}
              </p>
            </Link>
          ))}
        </div>
      </div>
    </main>
  );
}
