// Type-only imports, so `npm test` can load this file under
// `node --experimental-strip-types` (see scripts/navConfig.test.mjs).
import type { IconName } from "@/components/Icon";
import type { WorkspaceMode } from "@/lib/api";

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

export interface NavGroup {
  key: string;
  label: string;
  items: NavItem[];
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
   * Goals, People and Business profile. Defaults to "team".
   */
  mode?: WorkspaceMode;
}

const PEOPLE_DESCRIPTION =
  "Your roster — who the Executive coordinates with and their approval scopes.";

const GOALS_ITEM: NavItem = {
  href: "/goals",
  label: "Goals",
  icon: "flag",
  description: "What you're working towards, grouped by area — add, update and close goals.",
};

function companyGroup(isOnboarded: boolean): NavGroup {
  return {
    key: "company",
    label: "Company",
    items: [
      {
        href: "/departments",
        label: "Departments",
        icon: "grid",
        description: "Org units with goals, an authority level, and a specialist behind each.",
      },
      {
        href: "/people",
        label: "People",
        icon: "users",
        description: PEOPLE_DESCRIPTION,
      },
      {
        href: isOnboarded ? "/company-profile" : "/onboard",
        label: isOnboarded ? "Company profile" : "Set up company",
        icon: "building",
        description: "Your company's identity and strategy — set up once, edited any time.",
      },
    ],
  };
}

// Solo: the same destinations minus Departments (their goals live on /goals,
// grouped by area), with the copy speaking to one person.
function youGroup(isOnboarded: boolean): NavGroup {
  return {
    key: "you",
    label: "You",
    items: [
      GOALS_ITEM,
      {
        href: "/people",
        label: "People",
        icon: "users",
        description: "The people the Executive knows about — clients, partners, anyone you work with.",
      },
      {
        href: isOnboarded ? "/company-profile" : "/onboard",
        label: isOnboarded ? "Business profile" : "Set up your business",
        icon: "building",
        description: "Your business — what you offer, who you serve, your priorities.",
      },
    ],
  };
}

// Day-to-day navigation only. Power/admin tools live in the Settings
// area (see ADVANCED_ITEMS) so this list stays focused.
export function buildPrimaryNav({
  isOnboarded = true,
  reviewBadge = 0,
  mode = "team",
}: BuildOpts = {}): NavGroup[] {
  return [
    {
      key: "workspace",
      label: "Workspace",
      items: [
        {
          href: "/jobs",
          label: "Workflows",
          icon: "doc",
          description:
            "Workflows that produce a deliverable, plus the playbooks the Executive follows.",
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
      ],
    },
    mode === "solo" ? youGroup(isOnboarded) : companyGroup(isOnboarded),
    {
      key: "knowledge",
      label: "Knowledge",
      items: [
        {
          href: "/knowledge",
          label: "Knowledge base",
          icon: "book",
          badge: reviewBadge,
          description:
            "Upload company documents so the Executive can ground its answers in your context, and approve what it relies on.",
        },
      ],
    },
  ];
}

// Pinned, always-visible top-level destination — rendered as a standalone link
// directly beneath Briefing in BOTH navs (rail + chat-home sidebar), the same
// way Briefing is. Kept here as the single source so the two navs stay in sync.
export const PULSE_NAV_ITEM: NavItem = {
  href: "/memories",
  label: "Pulse",
  icon: "activity",
  description:
    "The Executive's memory and heartbeat — what it knows and the rhythm it runs on.",
};

// Single rail/sidebar entry that leads to the Settings hub.
export const SETTINGS_NAV_ITEM: NavItem = {
  href: "/settings",
  label: "Settings",
  icon: "cog",
  description: "Configuration, diagnostics, and power-user tools.",
};

// User Guide — pinned next to Settings in both nav footers so help is
// always one click away (it also stays listed on the Settings hub).
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

// Admin / power-user tools surfaced on the Settings hub page rather than
// in the primary nav — they aren't part of the day-to-day loop.
export const ADVANCED_ITEMS: NavItem[] = [
  {
    href: "/settings/status",
    label: "Setup status",
    icon: "check-circle",
    description:
      "A light for each part of your setup — AI key, sign-in, channels, schedule — and what to do about anything that isn't working.",
  },
  {
    href: "/council",
    label: "Agent Council",
    icon: "users",
    description:
      "Configure the agents — models, system prompts, deep-reasoning, and the Executive voice persona.",
  },
  {
    href: "/audit",
    label: "Audit log",
    icon: "doc-search",
    description:
      "Searchable event log of every chat turn, specialist consult, tool call, and scheduled action.",
  },
  {
    href: "/audit/usage",
    label: "Token usage",
    icon: "activity",
    description:
      "Aggregate token usage and cost across all sessions — totals, by day, and by model.",
  },
  {
    href: "/guide",
    label: "User Guide",
    icon: "info",
    description:
      "Plain-language overviews of every feature — what each one is and what it does.",
  },
  {
    href: "/architecture",
    label: "Architecture",
    icon: "grid",
    description: "Interactive reference docs explaining how the system is built.",
  },
  {
    href: "/demo",
    label: "Company Simulator",
    icon: "cog",
    description:
      "Load prebuilt company fixtures, snapshot your current data, or generate a new scenario with AI.",
  },
  {
    href: "/clients",
    label: "Client Companies",
    icon: "building",
    description:
      "Multi-client mode for fractional work — switch the live company between named client slots.",
  },
];

// Anchors the mobile bottom nav. ≤5 per Material guidance; "More" opens
// the drawer with the full menu. `/` lands on the briefing surface. Solo
// swaps People for Goals — the page a team of one visits most.
export function buildMobilePrimary(mode: WorkspaceMode = "team"): NavItem[] {
  return [
    { href: "/", label: "Briefing", icon: "clipboard", description: BRIEFING_DESCRIPTION },
    PULSE_NAV_ITEM,
    // `?new=1` signals the chat home to reset to a fresh chat and strip
    // the query — see the effect in app/page.tsx.
    { href: "/?new=1", label: "New chat", icon: "plus", description: NEW_CHAT_DESCRIPTION },
    mode === "solo"
      ? GOALS_ITEM
      : {
          href: "/people",
          label: "People",
          icon: "users",
          description: PEOPLE_DESCRIPTION,
        },
    {
      href: "/jobs",
      label: "Workflows",
      icon: "doc",
      description:
        "Multi-step workflows that produce a deliverable — board prep, GTM plans, reviews.",
    },
  ];
}

// Is `href` the active destination for `pathname`? Active on an exact match
// or anywhere below it (`/jobs` is active on `/jobs/runs/42`).
export function isNavActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
