"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import Icon from "@/components/Icon";
import { SETTINGS_GROUPS, SETTINGS_PAGES } from "@/components/shell/navConfig";
import { getHistory, getNotificationSettings } from "@/lib/api";
import { isNavItemActive, settingsNav, type SettingsNavGroup } from "@/lib/settingsNav";

// The frame around every Settings page. From `lg` up a menu of the Settings
// pages sits beside the page (lib/settingsNav), so the hub isn't a stop on
// the way to each one. On phones there's no menu: the hub is the list and
// each page has its way back.

interface SettingsShellValue {
  groups: SettingsNavGroup[];
}

const SettingsShellContext = createContext<SettingsShellValue | null>(null);

export function useSettingsShell(): SettingsShellValue | null {
  return useContext(SettingsShellContext);
}

/** The Settings pages this person can open, in the hub's groups. About you
 *  and Notifications are offered only to someone they answer for (null from
 *  their GET), as on the hub. */
function useSettingsNavGroups(): SettingsNavGroup[] {
  const [memoryOffered, setMemoryOffered] = useState(false);
  const [notificationsOffered, setNotificationsOffered] = useState(false);
  useEffect(() => {
    const ctrl = new AbortController();
    getHistory(undefined, ctrl.signal)
      .then((h) => setMemoryOffered(h !== null))
      .catch(() => {});
    getNotificationSettings(ctrl.signal)
      .then((n) => setNotificationsOffered(n !== null))
      .catch(() => {});
    return () => ctrl.abort();
  }, []);
  return useMemo(
    () =>
      settingsNav(
        SETTINGS_PAGES.filter(
          (p) => (p.id !== "memory" || memoryOffered) && (p.id !== "notifications" || notificationsOffered),
        ),
        SETTINGS_GROUPS,
      ),
    [memoryOffered, notificationsOffered],
  );
}

export default function SettingsShell({ children }: { children: ReactNode }) {
  const groups = useSettingsNavGroups();
  const value = useMemo(() => ({ groups }), [groups]);
  return (
    <SettingsShellContext.Provider value={value}>
      <div className="flex flex-1 min-h-0">
        <SettingsSidebar groups={groups} />
        <div className="flex min-w-0 flex-1 flex-col">{children}</div>
      </div>
    </SettingsShellContext.Provider>
  );
}

function SettingsSidebar({ groups }: { groups: SettingsNavGroup[] }) {
  const pathname = usePathname() ?? "";
  return (
    <nav
      aria-label="Settings"
      className="hidden lg:block w-64 flex-shrink-0 overflow-y-auto border-r border-line px-4 pt-8 pb-16"
    >
      <Link href="/settings" className="mx-2 block font-display text-xl font-bold tracking-tight text-fg">
        Settings
      </Link>
      {groups.map((group) => (
        <div key={group.id} className="mt-6">
          <h2 className="mx-2 mb-1.5 text-xs font-bold uppercase tracking-wider text-fg-subtle">{group.label}</h2>
          <ul className="space-y-0.5">
            {group.items.map((item) => {
              const active = isNavItemActive(item, pathname);
              return (
                <li key={item.key}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[15px] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 ${
                      active
                        ? "bg-surface-overlay font-medium text-fg"
                        : "text-fg-muted hover:bg-surface-overlay/60 hover:text-fg"
                    }`}
                  >
                    <Icon name={item.icon} size="w-[18px] h-[18px]" className={active ? "text-accent" : ""} />
                    <span className="truncate">{item.label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
