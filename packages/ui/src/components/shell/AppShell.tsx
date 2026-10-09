"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { AskOEProvider, useAskOE } from "@/components/askoe/AskOEContext";
import AskOEPanel from "@/components/askoe/AskOEPanel";
import Icon from "@/components/Icon";
import PausedBanner from "@/components/executive/PausedBanner";
import AppSidebar from "@/components/shell/AppSidebar";
import {
  buildMobilePrimary,
  hubForPath,
  isAdvancedPath,
  isDestinationActive,
  PROFILE_NAV,
  profileWording,
} from "@/components/shell/navConfig";
import HubTabs from "@/components/ui/HubTabs";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";

// Routes that own their full layout and should not be wrapped by the
// shell — sign-in, the onboarding wizard (full-screen flow), and the
// chat home (`/`), which keeps its own top bar and debug panel but
// renders the same `AppSidebar` as every other route.
const EXEMPT_PREFIXES = ["/signin", "/onboard", "/api"];
const EXEMPT_EXACT = new Set(["/"]);

// Human-readable labels for path segments shown in the breadcrumb.
// Dynamic segments (slugs / ids) are rendered raw and truncated by CSS.
// `company-profile` is the team label; TopBar names it for the mode and
// role, as the sidebar does.
const SEGMENT_LABELS: Record<string, string> = {
  today: "Today",
  review: "Review",
  proposals: "Proposals",
  people: "People",
  departments: "Departments",
  goals: "Goals",
  memories: "Pulse",
  knowledge: "Knowledge",
  jobs: "Workflows",
  artifacts: "Documents",
  chats: "Chats",
  runs: "Runs",
  new: "New",
  audit: "Audit log",
  usage: "Token usage",
  session: "Session",
  council: "Agent Council",
  architecture: "Architecture",
  "company-profile": "Company profile",
  demo: "Company Simulator",
  onboard: "Setup",
  watchlist: "Watch list",
  settings: "Settings",
  status: "Setup status",
  executive: "Your Executive",
  "act-as-me": "Act as me",
  delegate: "Delegate",
  "handle-it": "Handle it for me",
  "take-the-lead": "Take the lead",
  memory: "About you",
  workspace: "Workspace",
  advanced: "Advanced",
  tools: "Custom tools",
  about: "About",
  guide: "User Guide",
  clients: "Client Companies",
};

function labelFor(segment: string): string {
  return SEGMENT_LABELS[segment] ?? segment;
}

function isExempt(pathname: string): boolean {
  if (EXEMPT_EXACT.has(pathname)) return true;
  return EXEMPT_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { mode, role } = useWorkspace();

  if (isExempt(pathname)) {
    return <>{children}</>;
  }

  const segments = pathname.split("/").filter(Boolean);
  const hub = hubForPath(pathname, { mode, roleKind: role.role_kind });

  return (
    <AskOEProvider>
      <div className="flex h-full bg-surface text-fg">
        {/* Mobile backdrop */}
        {drawerOpen && (
          <div
            className="fixed top-8 bottom-0 left-0 right-0 bg-black/50 z-30 lg:hidden"
            onClick={() => setDrawerOpen(false)}
            aria-hidden="true"
          />
        )}

        {/* Left sidebar — fixed drawer on mobile, static on lg+. The rail
            is only reached post-onboarding, so the default `isOnboarded`
            is fine here. */}
        <AppSidebar
          pathname={pathname}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          breakpoint="lg"
        />

        {/* Main column */}
        <div className="flex-1 flex flex-col min-w-0">
          <TopBar
            segments={segments}
            onOpenDrawer={() => setDrawerOpen(true)}
          />
          <PausedBanner />
          {hub && <HubTabs hub={hub} pathname={pathname} />}
          {/* The shell slot has overflow-y-auto as a safety net for pages
              that don't manage their own scroll. Pages that DO own a
              scroll region (h-full + inner overflow-y-auto on <main>)
              still work — the inner constraint dominates and the outer
              slot stays a no-op. */}
          <div className="flex-1 min-h-0 flex flex-col overflow-y-auto">{children}</div>
          <MobileBottomNav pathname={pathname} />
        </div>

        {/* Ask OE — page-aware assistant panel, docked right on lg+,
            right sheet below. Renders nothing while closed. */}
        <AskOEPanel />
      </div>
    </AskOEProvider>
  );
}

function TopBar({
  segments,
  onOpenDrawer,
}: {
  segments: string[];
  onOpenDrawer: () => void;
}) {
  const { mode, role } = useWorkspace();
  const profileLabel = PROFILE_NAV[profileWording(mode, role.role_kind)].label;
  // Breadcrumb chain. Only the top-level section (segment[0]) is a real
  // route in this app — intermediate segments like `runs` in
  // `/jobs/runs/<id>` or `session` in `/audit/session/<id>` are not
  // navigable pages, so we render them as plain text to avoid linking
  // to a 404. Dynamic segments (slugs/uuids) also render as-is — pages
  // own their own H1 with the entity name.
  const pageCrumbs = segments.map((segment, idx) => {
    const linkable = idx === 0;
    const href = linkable ? "/" + segment : null;
    return {
      href,
      label: idx === 0 && segment === "company-profile" ? profileLabel : labelFor(segment),
    };
  });
  // The Advanced pages (Agent Council, Audit log, ...) live at their own
  // paths but are opened from Settings → Advanced: show that way back.
  const crumbs: { href: string | null; label: string }[] = isAdvancedPath("/" + segments.join("/"))
    ? [{ href: "/settings", label: "Settings" }, { href: "/settings/advanced", label: "Advanced" }, ...pageCrumbs]
    : pageCrumbs;

  return (
    <header className="h-14 border-b border-line flex items-center justify-between px-4 sm:px-6 flex-shrink-0 gap-3">
      <div className="flex items-center gap-2 min-w-0">
        <button
          type="button"
          aria-label="Open menu"
          onClick={onOpenDrawer}
          className="lg:hidden min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg cursor-pointer rounded-lg hover:bg-surface-overlay transition-colors"
        >
          <Icon name="menu" size="w-5 h-5" />
        </button>
        <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 min-w-0">
          {crumbs.length === 0 ? (
            <span className="font-display text-[15px] font-extrabold tracking-tight text-fg">Open Executive</span>
          ) : (
            crumbs.map((c, i) => {
              const isLast = i === crumbs.length - 1;
              // Phones show only the last two crumbs, so they don't all
              // truncate to a few letters each.
              const fromEnd = crumbs.length - i;
              return (
                <span
                  key={`${i}-${c.label}`}
                  className={`${fromEnd > 2 ? "hidden sm:flex" : "flex"} items-center gap-1.5 min-w-0`}
                >
                  {i > 0 && (
                    <Icon
                      name="chevron-right"
                      size="w-3 h-3"
                      className={`text-fg-subtle flex-shrink-0 ${fromEnd === 2 ? "hidden sm:block" : ""}`}
                    />
                  )}
                  {!isLast && c.href ? (
                    <Link
                      href={c.href}
                      className="text-sm text-fg-muted hover:text-fg transition-colors truncate"
                    >
                      {c.label}
                    </Link>
                  ) : (
                    <span
                      className={`text-sm truncate max-w-[200px] sm:max-w-none ${
                        isLast ? "font-medium text-fg" : "text-fg-muted"
                      }`}
                    >
                      {c.label}
                    </span>
                  )}
                </span>
              );
            })
          )}
        </nav>
      </div>
      <div className="flex items-center gap-3 flex-shrink-0">
        <AskOEButton />
      </div>
    </header>
  );
}

function AskOEButton() {
  const { open, toggle } = useAskOE();
  return (
    <button
      type="button"
      onClick={toggle}
      title="Ask OE about this page (Ctrl/Cmd + .)"
      aria-pressed={open}
      className={`flex min-h-10 items-center gap-1.5 px-3 rounded-lg text-sm font-semibold text-accent transition-colors cursor-pointer ${
        open ? "bg-accent/20" : "bg-accent/10 hover:bg-accent/15"
      }`}
    >
      <Icon name="sparkles" size="w-4 h-4" />
      <span className="sm:hidden">Ask</span>
      <span className="hidden sm:inline">Ask OE</span>
    </button>
  );
}

// Exported so the chat home (`/`) — which owns its own layout and is
// exempt from the shell — can render the same bottom bar every other
// route gets from the shell, keeping mobile nav consistent everywhere.
export function MobileBottomNav({
  pathname,
  hideFrom = "lg",
}: {
  pathname: string;
  // Breakpoint at which the bar disappears, matching the breakpoint where
  // the host layout's persistent sidebar/rail takes over. The shell rail
  // appears at `lg`; the chat home's sidebar appears at `md`, so that host
  // passes "md" to avoid showing both at tablet widths. Full literal class
  // strings (not interpolated) so Tailwind keeps them in the build.
  hideFrom?: "md" | "lg";
}) {
  const hideClass = hideFrom === "md" ? "md:hidden" : "lg:hidden";
  const { mode, role } = useWorkspace();
  // Everything not on the bar (Knowledge, Pulse, Settings, the account
  // menu) is in the sidebar the top bar's menu button opens.
  return (
    <nav
      aria-label="Primary"
      className={`${hideClass} h-[calc(4rem+env(safe-area-inset-bottom))] pb-[env(safe-area-inset-bottom)] border-t border-line bg-surface-elevated flex items-stretch flex-shrink-0`}
    >
      {buildMobilePrimary({ mode, roleKind: role.role_kind }).map((item) => {
        const active = isDestinationActive(item, pathname);
        const isNew = item.key === "new";
        return (
          <Link
            key={item.href}
            href={item.href}
            title={item.description}
            aria-current={active ? "page" : undefined}
            className={`flex-1 flex flex-col items-center justify-center gap-1 transition-colors ${
              active ? "text-accent" : "text-fg-muted hover:text-fg"
            }`}
          >
            {isNew ? (
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-accent-strong text-white shadow-sm">
                <Icon name={item.icon} size="w-5 h-5" />
              </span>
            ) : (
              <Icon name={item.icon} size="w-6 h-6" />
            )}
            <span className={`text-[11px] font-medium ${isNew ? "sr-only" : ""}`}>{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
