"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { AskOEProvider, useAskOE } from "@/components/askoe/AskOEContext";
import AskOEPanel from "@/components/askoe/AskOEPanel";
import Icon from "@/components/Icon";
import AppSidebar from "@/components/shell/AppSidebar";
import { isNavActive, MOBILE_PRIMARY } from "@/components/shell/navConfig";

// Routes that own their full layout and should not be wrapped by the
// shell — sign-in, the onboarding wizard (full-screen flow), and the
// chat home (`/`), which keeps its own top bar and debug panel but
// renders the same `AppSidebar` as every other route.
const EXEMPT_PREFIXES = ["/signin", "/onboard", "/api"];
const EXEMPT_EXACT = new Set(["/"]);

// Human-readable labels for path segments shown in the breadcrumb.
// Dynamic segments (slugs / ids) are rendered raw and truncated by CSS.
const SEGMENT_LABELS: Record<string, string> = {
  today: "Today",
  review: "Review",
  proposals: "Proposals",
  people: "People",
  departments: "Departments",
  skills: "Skills",
  memories: "Pulse",
  knowledge: "Knowledge base",
  jobs: "Jobs",
  artifacts: "Artifacts",
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

  if (isExempt(pathname)) {
    return <>{children}</>;
  }

  const segments = pathname.split("/").filter(Boolean);

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
          {/* The shell slot has overflow-y-auto as a safety net for pages
              that don't manage their own scroll. Pages that DO own a
              scroll region (h-full + inner overflow-y-auto on <main>)
              still work — the inner constraint dominates and the outer
              slot stays a no-op. */}
          <div className="flex-1 min-h-0 flex flex-col overflow-y-auto">{children}</div>
          <MobileBottomNav pathname={pathname} onOpenDrawer={() => setDrawerOpen(true)} />
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
  // Breadcrumb chain. Only the top-level section (segment[0]) is a real
  // route in this app — intermediate segments like `runs` in
  // `/jobs/runs/<id>` or `session` in `/audit/session/<id>` are not
  // navigable pages, so we render them as plain text to avoid linking
  // to a 404. Dynamic segments (slugs/uuids) also render as-is — pages
  // own their own H1 with the entity name.
  const crumbs = segments.map((segment, idx) => {
    const linkable = idx === 0;
    const href = linkable ? "/" + segment : null;
    return { href, label: labelFor(segment) };
  });

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
            <span className="text-sm text-fg-muted">Open Executive</span>
          ) : (
            crumbs.map((c, i) => {
              const isLast = i === crumbs.length - 1;
              return (
                <span key={`${i}-${c.label}`} className="flex items-center gap-1.5 min-w-0">
                  {i > 0 && (
                    <Icon name="chevron-right" size="w-3 h-3" className="text-fg-subtle flex-shrink-0" />
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
      className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
        open
          ? "bg-indigo-500/15 text-indigo-200"
          : "text-indigo-300 hover:text-indigo-200 hover:bg-surface-overlay"
      }`}
    >
      <Icon name="bolt" size="w-4 h-4" />
      <span className="hidden sm:inline">Ask OE</span>
    </button>
  );
}

// Exported so the chat home (`/`) — which owns its own layout and is
// exempt from the shell — can render the same bottom bar every other
// route gets from the shell, keeping mobile nav consistent everywhere.
export function MobileBottomNav({
  pathname,
  onOpenDrawer,
  hideFrom = "lg",
}: {
  pathname: string;
  onOpenDrawer: () => void;
  // Breakpoint at which the bar disappears, matching the breakpoint where
  // the host layout's persistent sidebar/rail takes over. The shell rail
  // appears at `lg`; the chat home's sidebar appears at `md`, so that host
  // passes "md" to avoid showing both at tablet widths. Full literal class
  // strings (not interpolated) so Tailwind keeps them in the build.
  hideFrom?: "md" | "lg";
}) {
  const hideClass = hideFrom === "md" ? "md:hidden" : "lg:hidden";
  return (
    <nav
      aria-label="Primary"
      className={`${hideClass} h-16 border-t border-line bg-surface-elevated flex items-stretch flex-shrink-0`}
    >
      {MOBILE_PRIMARY.map((item) => {
        const active = isNavActive(item.href, pathname);
        return (
          <Link
            key={item.href}
            href={item.href}
            title={item.description}
            className={`flex-1 flex flex-col items-center justify-center gap-0.5 transition-colors ${
              active ? "text-indigo-300" : "text-fg-muted hover:text-fg"
            }`}
          >
            <Icon name={item.icon} size="w-5 h-5" />
            <span className="text-[10px] font-medium">{item.label}</span>
          </Link>
        );
      })}
      <button
        type="button"
        onClick={onOpenDrawer}
        className="flex-1 flex flex-col items-center justify-center gap-0.5 text-fg-muted hover:text-fg transition-colors cursor-pointer"
        aria-label="Open menu"
      >
        <Icon name="menu" size="w-5 h-5" />
        <span className="text-[10px] font-medium">More</span>
      </button>
    </nav>
  );
}
