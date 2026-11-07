// The Settings menu beside every Settings page from `lg` up
// (components/settings/SettingsShell). It is built from the pages the hub
// would show the person (SETTINGS_PAGES, filtered the same way), in the hub's
// groups, so a page they can't open isn't listed here either.

import type { IconName } from "@/components/Icon";
import type {
  SettingsGroupDef,
  SettingsGroupId,
  SettingsPageDef,
  SettingsPageId,
} from "@/components/shell/navConfig";

export interface SettingsNavItem {
  key: SettingsPageId;
  label: string;
  href: string;
  icon: IconName;
  /** Other addresses that count as this entry's page. */
  alsoOn: string[];
}

export interface SettingsNavGroup {
  id: SettingsGroupId;
  label: string;
  items: SettingsNavItem[];
}

// Pages reached from an entry's page that have no entry of their own.
const ALSO_ON: Partial<Record<SettingsPageId, string[]>> = {
  executive: ["/settings/on-its-own"],
  advanced: ["/settings/tools", "/settings/status"],
};

/** The menu: `groups` (the hub's, SETTINGS_GROUPS) in order, each with its
 *  pages in the order given; a group with no page is left out. */
export function settingsNav(pages: SettingsPageDef[], groups: SettingsGroupDef[]): SettingsNavGroup[] {
  return groups
    .map((group) => ({
      id: group.id,
      label: group.label,
      items: pages
        .filter((page) => page.group === group.id)
        .map((page) => ({
          key: page.id,
          label: page.label,
          href: page.href,
          icon: page.icon,
          alsoOn: ALSO_ON[page.id] ?? [],
        })),
    }))
    .filter((group) => group.items.length > 0);
}

function onPath(href: string, pathname: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Whether `item` is the page open at `pathname`. */
export function isNavItemActive(item: SettingsNavItem, pathname: string): boolean {
  return [item.href, ...item.alsoOn].some((href) => onPath(href, pathname));
}

/** The first entry: where Settings opens beside the menu. */
export function firstNavHref(groups: SettingsNavGroup[]): string | null {
  return groups[0]?.items[0]?.href ?? null;
}
