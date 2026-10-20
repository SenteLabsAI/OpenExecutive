"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import BrandMark from "@/components/BrandMark";
import Icon, { IconName } from "@/components/Icon";
import UserBadge from "@/components/UserBadge";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  buildDestinations,
  isDestinationActive,
  isAdvancedPath,
  isNavActive,
  NEW_CHAT_DESCRIPTION,
  SETTINGS_NAV_ITEM,
} from "@/components/shell/navConfig";
import { getReviewStats } from "@/lib/api";

/**
 * Wiring the chat home passes in. `/` keeps its state in memory (mode,
 * active session), so its entries are buttons that call back into the page;
 * everywhere else the same entries are links (`/?new=1`, `/?session=<id>`)
 * that the home page consumes on mount.
 */
export interface SidebarHomeControls {
  mode: "briefing" | "chat";
  activeSessionId?: string;
  onBriefing: () => void;
  onNewChat: () => void;
}

interface AppSidebarProps {
  pathname: string;
  open: boolean;
  onClose: () => void;
  /** Width at which the sidebar stops being a drawer and sits in the layout. */
  breakpoint: "md" | "lg";
  isOnboarded?: boolean;
  companyName?: string;
  home?: SidebarHomeControls;
}

// Full literal class strings per breakpoint so Tailwind keeps them in the build.
const ASIDE_CLASSES = {
  md: "w-64 md:w-56 md:top-0 md:relative md:translate-x-0 md:transition-none",
  lg: "w-64 lg:w-56 lg:top-0 lg:relative lg:translate-x-0 lg:transition-none",
} as const;
const HIDE_FROM = { md: "md:hidden", lg: "lg:hidden" } as const;

export default function AppSidebar({
  pathname,
  open,
  onClose,
  breakpoint,
  isOnboarded,
  companyName,
  home,
}: AppSidebarProps) {
  const { mode, role } = useWorkspace();
  const [reviewBadge, setReviewBadge] = useState(0);

  // Knowledge base badge: items waiting in its review queue. Refetched on
  // navigation so approving items clears the badge once you move on, rather
  // than only on a full reload.
  useEffect(() => {
    getReviewStats()
      .then((s) => setReviewBadge(s.pending + s.needs_revision))
      .catch(() => {});
  }, [pathname]);

  const destinations = buildDestinations({ isOnboarded, reviewBadge, mode, roleKind: role.role_kind });
  const newChatActive = home ? home.mode === "chat" && home.activeSessionId === undefined : false;

  return (
    <aside
      className={`
        fixed top-8 bottom-0 left-0 z-40 flex-shrink-0
        border-r border-line flex flex-col bg-surface-elevated
        transform transition-transform duration-200
        ${ASIDE_CLASSES[breakpoint]}
        ${open ? "translate-x-0" : "-translate-x-full"}
      `}
    >
      <div className="px-4 py-4 flex items-center justify-between flex-shrink-0">
        <SidebarEntry
          href={home ? undefined : "/"}
          onClick={home ? home.onBriefing : onClose}
          ariaLabel="Back to home"
          className="flex items-center gap-2.5 min-w-0 text-left text-fg cursor-pointer hover:opacity-80 transition-opacity"
        >
          <span className="flex-shrink-0">
            <BrandMark size="sm" />
          </span>
          <span className="min-w-0">
            <span className="block font-display text-[17px] font-extrabold tracking-tight truncate">Open Executive</span>
            {companyName && (
              <span className="block text-xs text-fg-muted truncate">{companyName}</span>
            )}
          </span>
        </SidebarEntry>
        <button
          type="button"
          aria-label="Close menu"
          onClick={onClose}
          className={`${HIDE_FROM[breakpoint]} min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg cursor-pointer rounded-lg hover:bg-surface-overlay transition-colors`}
        >
          <Icon name="close" size="w-5 h-5" />
        </button>
      </div>

      <div className="px-3 pb-2 flex-shrink-0">
        <SidebarEntry
          href={home ? undefined : "/?new=1"}
          onClick={home ? home.onNewChat : onClose}
          title={NEW_CHAT_DESCRIPTION}
          ariaCurrent={newChatActive}
          className="w-full h-11 rounded-xl bg-accent-strong text-white text-[15px] font-semibold flex items-center justify-center gap-2 shadow-sm hover:bg-accent-strong/90 transition-colors cursor-pointer"
        >
          <Icon name="plus" size="w-5 h-5" />
          New chat
        </SidebarEntry>
      </div>

      {/* The six places. Everything else is a tab inside one of them, or a
          tool on Settings. */}
      <nav aria-label="Main" className="flex-1 min-h-0 overflow-y-auto px-3 py-2">
        <div className="space-y-1">
          {destinations.map((d) => {
            const isHome = d.key === "home";
            const active =
              isHome && home ? home.mode === "briefing" : isDestinationActive(d, pathname);
            return (
              <NavRow
                key={d.key}
                href={isHome && home ? undefined : d.href}
                onClick={isHome && home ? home.onBriefing : onClose}
                label={d.label}
                icon={d.icon}
                description={d.description}
                active={active}
                badge={d.badge}
              />
            );
          })}
        </div>
      </nav>

      <div className="px-3 pb-2 pt-2 border-t border-line flex-shrink-0">
        <NavRow
          href={SETTINGS_NAV_ITEM.href}
          onClick={onClose}
          label={SETTINGS_NAV_ITEM.label}
          icon={SETTINGS_NAV_ITEM.icon}
          description={SETTINGS_NAV_ITEM.description}
          active={isNavActive(SETTINGS_NAV_ITEM.href, pathname) || isAdvancedPath(pathname)}
        />
      </div>

      <UserBadge variant="sidebar" />
    </aside>
  );
}

// A link when `href` is given, otherwise a button — the chat home drives
// its in-page entries through callbacks, every other route through URLs.
function SidebarEntry({
  href,
  onClick,
  title,
  ariaLabel,
  ariaCurrent,
  className,
  children,
}: {
  href?: string;
  onClick: () => void;
  title?: string;
  ariaLabel?: string;
  ariaCurrent?: boolean;
  className: string;
  children: React.ReactNode;
}) {
  const current = ariaCurrent ? ("page" as const) : undefined;
  if (href) {
    return (
      <Link
        href={href}
        onClick={onClick}
        title={title}
        aria-label={ariaLabel}
        aria-current={current}
        className={className}
      >
        {children}
      </Link>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={ariaLabel}
      aria-current={current}
      className={className}
    >
      {children}
    </button>
  );
}

function NavRow({
  href,
  onClick,
  label,
  icon,
  description,
  active,
  badge,
}: {
  href?: string;
  onClick: () => void;
  label: string;
  icon: IconName;
  /** Tooltip explaining the destination — shown via `title` on hover. */
  description?: string;
  active: boolean;
  badge?: number;
}) {
  const tone = active
    ? "bg-accent/10 text-accent font-semibold"
    : "text-fg-muted font-medium hover:text-fg hover:bg-surface-overlay";
  return (
    <SidebarEntry
      href={href}
      onClick={onClick}
      title={description}
      ariaCurrent={active}
      className={`w-full text-left px-3 h-11 rounded-xl flex items-center gap-3 text-[15px] transition-colors cursor-pointer ${tone}`}
    >
      <Icon name={icon} size="w-5 h-5" />
      <span className="flex-1 truncate">{label}</span>
      {badge != null && badge > 0 && <Badge count={badge} />}
    </SidebarEntry>
  );
}

function Badge({ count }: { count: number }) {
  return (
    <span className="text-xs bg-amber-500 text-white rounded-full px-2 py-0.5 leading-none font-semibold">
      {count}
    </span>
  );
}
