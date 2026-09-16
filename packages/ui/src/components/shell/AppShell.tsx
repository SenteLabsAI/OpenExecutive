"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { AskOEProvider, useAskOE } from "@/components/askoe/AskOEContext";
import AskOEPanel from "@/components/askoe/AskOEPanel";
import BrandMark from "@/components/BrandMark";
import Icon, { IconName } from "@/components/Icon";
import UserBadge from "@/components/UserBadge";
import {
  BRIEFING_DESCRIPTION,
  buildPrimaryNav,
  GUIDE_NAV_ITEM,
  MOBILE_PRIMARY,
  NEW_CHAT_DESCRIPTION,
  PULSE_NAV_ITEM,
  SETTINGS_NAV_ITEM,
} from "@/components/shell/navConfig";

// Routes that own their full layout and should not be wrapped by the
// shell — sign-in, the onboarding wizard (full-screen flow), and the
// chat home (`/`) which already provides its own sidebar with
// recent-session controls. Every other route renders inside the shell.
const EXEMPT_PREFIXES = ["/signin", "/onboard", "/api"];
const EXEMPT_EXACT = new Set(["/"]);

// Primary nav comes from the shared config (the same source the chat
// home uses) so the two navs can never drift. The rail is only reached
// post-onboarding, so the default `isOnboarded` is fine here.
const NAV_GROUPS = buildPrimaryNav();

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

// Is `href` the active section for the current pathname? Active when
// pathname matches exactly or sits below href as a sub-route.
function isActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
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

        {/* Left rail — fixed drawer on mobile, static on lg+ */}
        <Rail
          pathname={pathname}
          drawerOpen={drawerOpen}
          onClose={() => setDrawerOpen(false)}
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

function Rail({
  pathname,
  drawerOpen,
  onClose,
}: {
  pathname: string;
  drawerOpen: boolean;
  onClose: () => void;
}) {
  return (
    <aside
      className={`
        fixed top-8 bottom-0 left-0 z-40 w-64 lg:w-56 lg:top-0 flex-shrink-0
        border-r border-line flex flex-col bg-surface-elevated
        transform transition-transform duration-200
        lg:relative lg:translate-x-0 lg:transition-none
        ${drawerOpen ? "translate-x-0" : "-translate-x-full"}
      `}
    >
      <div className="px-4 py-4 border-b border-line flex items-center justify-between flex-shrink-0">
        <Link
          href="/"
          onClick={onClose}
          className="flex items-center gap-2.5 min-w-0 text-fg hover:opacity-80 transition-opacity"
        >
          <BrandMark size="sm" />
          <span className="text-sm font-semibold truncate">Open Executive</span>
        </Link>
        <button
          type="button"
          aria-label="Close menu"
          onClick={onClose}
          className="lg:hidden min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg cursor-pointer rounded-lg hover:bg-surface-overlay transition-colors"
        >
          <Icon name="close" size="w-5 h-5" />
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-4">
        {/* New chat — the single most-used action in a chat-first
            product, so it lives at the top of the rail on every route.
            `/?new=1` is consumed by the chat home, which resets state
            and strips the query. Never marked active (it's an action,
            not a destination). */}
        <RailLink
          href="/?new=1"
          label="New chat"
          icon="plus"
          description={NEW_CHAT_DESCRIPTION}
          active={false}
          onClick={onClose}
          accent
        />
        {/* Briefing — `/` lands on the briefing surface (main's
            briefing-first refactor); the chat is reached from there via
            "New chat" or by selecting a recent session. Labelled
            accordingly so the rail doesn't promise something else. */}
        <RailLink
          href="/"
          label="Briefing"
          icon="clipboard"
          description={BRIEFING_DESCRIPTION}
          active={pathname === "/"}
          onClick={onClose}
        />
        {/* Pulse — pinned beside Briefing as an always-visible destination,
            not buried in the Knowledge group. */}
        <RailLink
          href={PULSE_NAV_ITEM.href}
          label={PULSE_NAV_ITEM.label}
          icon={PULSE_NAV_ITEM.icon}
          description={PULSE_NAV_ITEM.description}
          active={isActive(PULSE_NAV_ITEM.href, pathname)}
          onClick={onClose}
        />

        {NAV_GROUPS.map((group) => (
          <div key={group.key}>
            <p className="px-3 mb-1 text-[10px] font-semibold uppercase tracking-widest text-fg-subtle">
              {group.label}
            </p>
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <RailLink
                  key={item.href}
                  href={item.href}
                  label={item.label}
                  icon={item.icon}
                  description={item.description}
                  active={isActive(item.href, pathname)}
                  onClick={onClose}
                />
              ))}
            </div>
          </div>
        ))}
      </nav>

      {/* Footer — User Guide (always-visible help) and Settings (the hub
          for admin/power tools), kept out of the primary groups above so
          day-to-day nav stays focused. Pinned just above the user badge. */}
      <div className="px-2 pb-1 border-t border-line pt-2 space-y-0.5">
        <RailLink
          href={GUIDE_NAV_ITEM.href}
          label={GUIDE_NAV_ITEM.label}
          icon={GUIDE_NAV_ITEM.icon}
          description={GUIDE_NAV_ITEM.description}
          active={isActive(GUIDE_NAV_ITEM.href, pathname)}
          onClick={onClose}
        />
        <RailLink
          href={SETTINGS_NAV_ITEM.href}
          label={SETTINGS_NAV_ITEM.label}
          icon={SETTINGS_NAV_ITEM.icon}
          description={SETTINGS_NAV_ITEM.description}
          active={isActive(SETTINGS_NAV_ITEM.href, pathname)}
          onClick={onClose}
        />
      </div>

      <UserBadge variant="sidebar" />
    </aside>
  );
}

function RailLink({
  href,
  label,
  icon,
  description,
  active,
  onClick,
  accent,
}: {
  href: string;
  label: string;
  icon: IconName;
  /** Tooltip explaining the destination — shown via `title` on hover. */
  description?: string;
  active: boolean;
  onClick: () => void;
  accent?: boolean;
}) {
  // Active styling: filled overlay + bolder text. Accent (used for the
  // Chat entry) gets a subtle indigo tint to mark the primary action.
  const base =
    "px-3 py-2 min-h-touch rounded-lg flex items-center gap-2.5 text-sm transition-colors cursor-pointer";
  const tone = active
    ? accent
      ? "bg-indigo-500/15 text-indigo-200 font-medium"
      : "bg-surface-overlay text-fg font-medium"
    : accent
      ? "text-indigo-300 hover:text-indigo-200 hover:bg-surface-overlay"
      : "text-fg-muted hover:text-fg hover:bg-surface-overlay";
  return (
    <Link href={href} onClick={onClick} title={description} className={`${base} ${tone}`}>
      <Icon name={icon} size="w-4 h-4" />
      <span className="flex-1 truncate">{label}</span>
    </Link>
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
        const active = isActive(item.href, pathname);
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
