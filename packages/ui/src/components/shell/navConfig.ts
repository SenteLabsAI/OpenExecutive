// Type-only imports, so `npm test` can load this file under
// `node --experimental-strip-types` (see scripts/navConfig.test.mjs).
import type { IconName } from "@/components/Icon";
import type { RoleKind, WorkspaceMode } from "@/lib/api";

// Single source of truth for the app's navigation. The one sidebar
// (`components/shell/AppSidebar.tsx`, rendered by both the chat home and
// the AppShell) and the mobile bottom bar build their menus from here.
// When adding a destination, add it ONCE in this file.

export interface NavItem {
  href: string;
  label: string;
  icon: IconName;
  /**
   * One-line plain-language explanation of the destination, surfaced as a
   * tooltip in the rail/sidebar and as card copy on the Settings hub.
   * Required so every new destination ships with an explanation.
   */
  description: string;
  /** Optional pending-count badge (e.g. items awaiting review). */
  badge?: number;
}

interface BuildOpts {
  /**
   * When false, the Company-profile entry points at the onboarding
   * wizard and is relabelled "Set up company". The chat home knows the
   * onboarding state from `/health`; the rail assumes onboarded (its
   * routes are only reachable post-setup).
   */
  isOnboarded?: boolean;
  /** Pending + needs-revision count shown on the Review entry. */
  reviewBadge?: number;
  /**
   * "solo" (one person using Open Executive just for themselves) swaps the
   * Company group — Departments, People, Company profile — for "You":
   * Goals, People and the profile, named for `roleKind` (see
   * `profileWording`). Defaults to "team".
   */
  mode?: WorkspaceMode;
  /**
   * The principal's role kind from the workspace settings. Only solo reads
   * it; null when unset or hidden (GET /workspace returns no role to anyone
   * but the principal).
   */
  roleKind?: RoleKind | null;
}

// What the profile at /company-profile is called. A team's is its company.
// In solo it follows the principal's role: an owner's is their business;
// anyone else's is their work — the organisation they work in, who it
// serves and their priorities. An unset role, or one the caller can't see,
// reads as "work", so nobody is told they run a business they don't.
// The profile page, its breadcrumb and onboarding use the same rule.
export type ProfileWording = "company" | "business" | "work";

export function profileWording(
  mode: WorkspaceMode = "team",
  roleKind: RoleKind | null = null,
): ProfileWording {
  if (mode !== "solo") return "company";
  return roleKind === "owner" ? "business" : "work";
}

// The profile's nav entry: its label once set up, before setup (it then
// points at /onboard), and the tooltip / Settings-card description.
export const PROFILE_NAV: Record<
  ProfileWording,
  { label: string; setupLabel: string; description: string }
> = {
  company: {
    label: "Company profile",
    setupLabel: "Set up company",
    description: "Your company's identity and strategy — set up once, edited any time.",
  },
  business: {
    label: "Business profile",
    setupLabel: "Set up your business",
    description: "Your business — what you offer, who you serve, your priorities.",
  },
  work: {
    label: "Your work",
    setupLabel: "Set up your work",
    description: "Your work — the organisation you work in, who it serves, your priorities.",
  },
};

function profileItem(wording: ProfileWording, isOnboarded: boolean): NavItem {
  const copy = PROFILE_NAV[wording];
  return {
    href: isOnboarded ? "/company-profile" : "/onboard",
    label: isOnboarded ? copy.label : copy.setupLabel,
    icon: "building",
    description: copy.description,
  };
}

const PEOPLE_DESCRIPTION =
  "Your roster — who the Executive coordinates with and their approval scopes.";

const GOALS_ITEM: NavItem = {
  href: "/goals",
  label: "Goals",
  icon: "flag",
  description: "What you're working towards, grouped by area — add, update and close goals.",
};

// The Company hub's tabs in a team: who's on it, what you're aiming for,
// how it's organised, and the profile.
function companyTabs(isOnboarded: boolean): NavItem[] {
  return [
    { href: "/people", label: "People", icon: "users", description: PEOPLE_DESCRIPTION },
    {
      ...GOALS_ITEM,
      description: "Every department's goals in one place — add, update and close them.",
    },
    {
      href: "/departments",
      label: "Departments",
      icon: "grid",
      description: "Your departments, their goals, how much each may decide on its own, and the expert behind each.",
    },
    profileItem("company", isOnboarded),
  ];
}

// Solo: the same destinations minus Departments (their goals live on /goals,
// grouped by area), with the copy speaking to one person. Goals come first:
// it's the page a team of one visits most.
function youTabs(isOnboarded: boolean, roleKind: RoleKind | null): NavItem[] {
  return [
    GOALS_ITEM,
    {
      href: "/people",
      label: "People",
      icon: "users",
      description: "The people the Executive knows about — clients, partners, anyone you work with.",
    },
    profileItem(profileWording("solo", roleKind), isOnboarded),
  ];
}

const WORK_TABS: NavItem[] = [
  {
    href: "/jobs",
    label: "Workflows",
    icon: "doc",
    description: "Workflows that produce a deliverable, plus the playbooks the Executive follows.",
  },
  {
    href: "/artifacts",
    label: "Documents",
    icon: "book",
    description: "Your library of finished documents — drafts and workflow outputs.",
  },
  {
    href: "/watchlist",
    label: "Watch list",
    icon: "eye",
    description: "External monitors — tickers, feeds, status pages — that raise alerts.",
  },
];

/**
 * A hub: one menu entry that holds several pages, shown as a row of tabs at
 * the top of each of them (components/ui/HubTabs.tsx). The menu entry opens
 * the first tab.
 */
export interface Hub {
  key: "work" | "company" | "you";
  label: string;
  icon: IconName;
  description: string;
  tabs: NavItem[];
}

export function buildHubs({
  isOnboarded = true,
  mode = "team",
  roleKind = null,
}: BuildOpts = {}): Hub[] {
  const work: Hub = {
    key: "work",
    label: "Work",
    icon: "briefcase",
    description: "Workflows, finished documents and the watch list.",
    tabs: WORK_TABS,
  };
  const people: Hub =
    mode === "solo"
      ? {
          key: "you",
          label: "You",
          icon: "users",
          description: "Your goals, the people you work with, and your profile.",
          tabs: youTabs(isOnboarded, roleKind),
        }
      : {
          key: "company",
          label: "Company",
          icon: "building",
          description: "People, goals, departments and the company profile.",
          tabs: companyTabs(isOnboarded),
        };
  return [work, people];
}

// The hub a page belongs to, or null for a page that isn't in one.
export function hubForPath(pathname: string, opts: BuildOpts = {}): Hub | null {
  return (
    buildHubs(opts).find((hub) => hub.tabs.some((tab) => isNavActive(tab.href, pathname))) ?? null
  );
}

export const PULSE_NAV_ITEM: NavItem = {
  href: "/memories",
  label: "Pulse",
  icon: "activity",
  description:
    "The Executive's memory and heartbeat — what it knows and the rhythm it runs on.",
};

/** A main-menu entry. A hub's entry is active on any of its tabs. */
export interface Destination extends NavItem {
  key: string;
  /** Pages that light this entry up, besides `href` itself. */
  alsoActiveOn?: string[];
}

// The main menu, the same in the sidebar on every page: six places, then
// Settings at the bottom. Everything else is a tab inside one of them or a
// tool under Settings.
export function buildDestinations({
  isOnboarded = true,
  reviewBadge = 0,
  mode = "team",
  roleKind = null,
}: BuildOpts = {}): Destination[] {
  const hubs = buildHubs({ isOnboarded, mode, roleKind });
  const hubEntry = (hub: Hub): Destination => ({
    key: hub.key,
    href: hub.tabs[0].href,
    label: hub.label,
    icon: hub.icon,
    description: hub.description,
    alsoActiveOn: hub.tabs.slice(1).map((t) => t.href),
  });
  return [
    { key: "home", href: "/", label: "Home", icon: "home", description: BRIEFING_DESCRIPTION },
    {
      key: "chats",
      href: "/chats",
      label: "Chats",
      icon: "chat",
      description: "Every conversation, searchable — including Slack, Telegram and Discord.",
    },
    ...hubs.map(hubEntry),
    {
      key: "knowledge",
      href: "/knowledge",
      label: "Knowledge",
      icon: "book",
      badge: reviewBadge,
      description:
        "Upload company documents so the Executive can ground its answers in your context, and approve what it relies on.",
    },
    { key: "pulse", ...PULSE_NAV_ITEM },
  ];
}

export function isDestinationActive(dest: Destination, pathname: string): boolean {
  return [dest.href, ...(dest.alsoActiveOn ?? [])].some((href) => isNavActive(href, pathname));
}

// Single rail/sidebar entry that leads to the Settings hub.
export const SETTINGS_NAV_ITEM: NavItem = {
  href: "/settings",
  label: "Settings",
  icon: "cog",
  description: "Configuration, diagnostics, and power-user tools.",
};

// User Guide — in the account menu at the foot of the sidebar so help is
// always one click away (it also stays listed under Settings → Advanced).
export const GUIDE_NAV_ITEM: NavItem = {
  href: "/guide",
  label: "User Guide",
  icon: "info",
  description: "Plain-language overviews of every feature — what each one is and what it does.",
};

// Descriptions for the two chat-home actions that aren't NavItems (they
// toggle modes rather than navigate). Shared by MOBILE_PRIMARY, the rail
// (AppShell), and the chat-home sidebar so the copy lives once.
export const NEW_CHAT_DESCRIPTION = "Start a fresh conversation with the Executive.";
export const BRIEFING_DESCRIPTION =
  "Land on a daily brief of what's happened and what needs you.";

// Where a Settings tool sits on Settings → Advanced: what you open to check
// on the install, to change how it runs, or to learn how it works.
export type AdvancedGroupKey = "diagnose" | "configure" | "learn";

export interface AdvancedItem extends NavItem {
  group: AdvancedGroupKey;
}

export const ADVANCED_GROUPS: { key: AdvancedGroupKey; label: string }[] = [
  { key: "diagnose", label: "Check & diagnose" },
  { key: "configure", label: "Configure" },
  { key: "learn", label: "Learn" },
];

// Admin / power-user tools surfaced on Settings → Advanced rather than
// in the primary nav — they aren't part of the day-to-day loop.
export const ADVANCED_ITEMS: AdvancedItem[] = [
  {
    href: "/settings/status",
    label: "Setup status",
    icon: "check-circle",
    group: "diagnose",
    description:
      "A light for each part of your setup — AI key, sign-in, channels, schedule — and what to do about anything that isn't working.",
  },
  {
    href: "/council",
    label: "Agent Council",
    icon: "users",
    group: "configure",
    description:
      "Choose how thorough answers are, and change each agent's model, instructions and the Executive's voice.",
  },
  {
    href: "/audit",
    label: "Audit log",
    icon: "doc-search",
    group: "diagnose",
    description:
      "A searchable record of everything the Executive did: each chat, each question it passed to an expert, each tool it used and each scheduled job.",
  },
  {
    href: "/audit/usage",
    label: "Token usage",
    icon: "activity",
    group: "diagnose",
    description:
      "What the AI has cost: the total, each day, and for each model.",
  },
  {
    href: "/guide",
    label: "User Guide",
    icon: "info",
    group: "learn",
    description:
      "Plain-language overviews of every feature — what each one is and what it does.",
  },
  {
    href: "/architecture",
    label: "Architecture",
    icon: "grid",
    group: "learn",
    description: "Interactive reference docs explaining how the system is built.",
  },
  {
    href: "/demo",
    label: "Company Simulator",
    icon: "cog",
    group: "configure",
    description:
      "Load a ready-made demo company, save a copy of your current data, or have AI make up a new one.",
  },
  {
    href: "/clients",
    label: "Client Companies",
    icon: "building",
    group: "configure",
    description:
      "For fractional work: switch between the companies you work for.",
  },
];

// The tools as Settings → Advanced lists them: by group, in ADVANCED_GROUPS
// order, each keeping its ADVANCED_ITEMS order within the group.
export function advancedItemsByGroup(): {
  key: AdvancedGroupKey;
  label: string;
  items: AdvancedItem[];
}[] {
  return ADVANCED_GROUPS.map((group) => ({
    ...group,
    items: ADVANCED_ITEMS.filter((item) => item.group === group.key),
  }));
}

// The Settings hub's tiles, in hub order. Each opens a short page of its
// own at `href`. "act-as-me" shows only for someone who can have Act as me
// (the hub drops it when the card is hidden), "memory" only for someone with
// notes to keep (signed in and on the People list). `hashes` are the anchors the
// old one-page Settings used (`/settings#workspace`): links that still
// carry one land on the matching page (see settingsPageForHash).
export type SettingsPageId = "executive" | "act-as-me" | "memory" | "workspace" | "advanced" | "about";

export interface SettingsPageDef {
  id: SettingsPageId;
  label: string;
  href: string;
  icon: IconName;
  /** What's on the page, as the hub tile says it. */
  description: string;
  hashes: string[];
}

export const SETTINGS_PAGES: SettingsPageDef[] = [
  {
    id: "executive",
    label: "Your Executive",
    href: "/settings/executive",
    icon: "cog",
    description: "Pause it, what it does without asking you, and the voice it answers in.",
    hashes: ["executive", "on-its-own"],
  },
  {
    id: "act-as-me",
    label: "Act as me",
    href: "/settings/act-as-me",
    icon: "mail",
    description: "Your mailbox, drafts and replies sent as you, and how you write.",
    hashes: ["act-as-me"],
  },
  {
    id: "memory",
    label: "About you",
    href: "/settings/memory",
    icon: "user",
    description: "What the Executive has learned about you, and the private notes it keeps.",
    hashes: ["memory"],
  },
  {
    id: "workspace",
    label: "Workspace",
    href: "/settings/workspace",
    icon: "building",
    description: "Just you or your team, time zone, email domains.",
    hashes: ["workspace"],
  },
  {
    id: "advanced",
    label: "Advanced",
    href: "/settings/advanced",
    icon: "grid",
    description: "Agent Council, audit log, token usage, setup status, simulator, guide.",
    // The old Tools section, and the anchors of its groups.
    hashes: ["tools", ...ADVANCED_GROUPS.map((g) => `tools-${g.key}`)],
  },
  {
    id: "about",
    label: "About",
    href: "/settings/about",
    icon: "info",
    description: "The version this install runs, and whether a newer one is out.",
    hashes: ["about"],
  },
];

// The page an old `/settings#<hash>` link meant, or null for no hash or one
// that never named a section. Takes the hash with or without its "#".
export function settingsPageForHash(hash: string): SettingsPageDef | null {
  let id = hash.replace(/^#/, "");
  try {
    id = decodeURIComponent(id);
  } catch {
    // A malformed escape: match it as typed.
  }
  id = id.trim();
  if (!id) return null;
  return SETTINGS_PAGES.find((p) => p.hashes.includes(id)) ?? null;
}

// The phone's bottom bar: five, with New chat in the middle. Knowledge,
// Pulse and Settings are in the menu the top bar's button opens.
// `?new=1` signals the chat home to reset to a fresh chat and strip the
// query — see the effect in app/page.tsx.
export function buildMobilePrimary(opts: BuildOpts = {}): Destination[] {
  const all = buildDestinations(opts);
  const pick = (key: string) => all.find((d) => d.key === key)!;
  const newChat: Destination = {
    key: "new",
    href: "/?new=1",
    label: "New chat",
    icon: "plus",
    description: NEW_CHAT_DESCRIPTION,
  };
  const people = all.find((d) => d.key === "company" || d.key === "you")!;
  return [pick("home"), pick("chats"), newChat, pick("work"), people];
}

// Is `href` the active destination for `pathname`? Active on an exact match
// or anywhere below it (`/jobs` is active on `/jobs/runs/42`).
/** Whether a page is one of the Advanced items (Agent Council, Audit log,
 * ...): they live at their own top-level paths but are opened from Settings →
 * Advanced, so the top bar and sidebar place them under Settings. */
export function isAdvancedPath(pathname: string): boolean {
  return ADVANCED_ITEMS.some((item) => isNavActive(item.href, pathname));
}

export function isNavActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
