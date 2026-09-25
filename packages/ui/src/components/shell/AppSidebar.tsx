"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import BrandMark from "@/components/BrandMark";
import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import Icon, { IconName } from "@/components/Icon";
import { useSessions } from "@/components/sessions/SessionsContext";
import UserBadge from "@/components/UserBadge";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  BRIEFING_DESCRIPTION,
  buildPrimaryNav,
  GUIDE_NAV_ITEM,
  isNavActive,
  NEW_CHAT_DESCRIPTION,
  PULSE_NAV_ITEM,
  SETTINGS_NAV_ITEM,
} from "@/components/shell/navConfig";
import { getReviewStats } from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import { recentWebChats, sessionTitle } from "@/lib/sessionChannel";

// The sidebar keeps history short on purpose: the full, searchable list
// (with Slack/Telegram/Discord conversations) lives on /chats.
const RECENT_LIMIT = 7;
const COLLAPSED_STORAGE_KEY = "oe.sidebar.collapsedGroups";

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
  onSelectSession: (sessionId: string) => void;
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
  const { sessions } = useSessions();
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

  const groups = buildPrimaryNav({ isOnboarded, reviewBadge, mode, roleKind: role.role_kind });
  const recent = recentWebChats(sessions, RECENT_LIMIT);

  const briefingActive = home ? home.mode === "briefing" : isNavActive("/", pathname);
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
      <div className="px-4 py-4 border-b border-line flex items-center justify-between flex-shrink-0">
        <SidebarEntry
          href={home ? undefined : "/"}
          onClick={home ? home.onBriefing : onClose}
          ariaLabel="Back to briefing"
          className="flex items-center gap-2.5 min-w-0 text-left text-fg cursor-pointer hover:opacity-80 transition-opacity"
        >
          <span className="flex-shrink-0">
            <BrandMark size="sm" />
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold truncate">Open Executive</span>
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

      <nav className="flex-1 min-h-0 overflow-y-auto px-2 py-3">
        <div className="space-y-0.5">
          <NavRow
            href={home ? undefined : "/?new=1"}
            onClick={home ? home.onNewChat : onClose}
            label="New chat"
            icon="plus"
            description={NEW_CHAT_DESCRIPTION}
            active={newChatActive}
            accent
          />
          <NavRow
            href={home ? undefined : "/"}
            onClick={home ? home.onBriefing : onClose}
            label="Briefing"
            icon="clipboard"
            description={BRIEFING_DESCRIPTION}
            active={briefingActive}
          />
          <NavRow
            href={PULSE_NAV_ITEM.href}
            onClick={onClose}
            label={PULSE_NAV_ITEM.label}
            icon={PULSE_NAV_ITEM.icon}
            description={PULSE_NAV_ITEM.description}
            active={isNavActive(PULSE_NAV_ITEM.href, pathname)}
          />
        </div>

        <NavGroups groups={groups} pathname={pathname} onNavigate={onClose} />

        <RecentChats
          recent={recent}
          hasAny={sessions.length > 0}
          activeSessionId={home?.activeSessionId}
          allChatsActive={isNavActive("/chats", pathname)}
          onSelect={home?.onSelectSession}
          onNavigate={onClose}
        />
      </nav>

      {/* Footer — the Executive's pause switch (here, in the shared sidebar,
          so it is one click away on every route, the chat home included),
          User Guide (always-visible help) and Settings (the hub for
          admin/power tools), kept out of the primary groups above so
          day-to-day nav stays focused. */}
      <div className="px-2 pb-1 border-t border-line pt-2 space-y-0.5 flex-shrink-0">
        <ExecutiveRunSwitch variant="sidebar" />
        {[GUIDE_NAV_ITEM, SETTINGS_NAV_ITEM].map((item) => (
          <NavRow
            key={item.href}
            href={item.href}
            onClick={onClose}
            label={item.label}
            icon={item.icon}
            description={item.description}
            active={isNavActive(item.href, pathname)}
          />
        ))}
      </div>

      <UserBadge variant="sidebar" />
    </aside>
  );
}

function NavGroups({
  groups,
  pathname,
  onNavigate,
}: {
  groups: ReturnType<typeof buildPrimaryNav>;
  pathname: string;
  onNavigate: () => void;
}) {
  // Explicit user toggles, persisted so they survive the remount between the
  // chat home and the shell. Absent keys fall back to "open the first group
  // and the one holding the current page".
  const [collapsedOverride, setCollapsedOverride] = useState<Record<string, boolean>>({});

  // Read after mount, not in the initial state, so the prerendered HTML and
  // the first client render agree.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : null;
      if (parsed && typeof parsed === "object") {
        const stored: Record<string, boolean> = {};
        for (const [k, v] of Object.entries(parsed)) {
          if (typeof v === "boolean") stored[k] = v;
        }
        setCollapsedOverride(stored);
      }
    } catch {
      // Storage blocked or corrupt — fall back to the defaults.
    }
  }, []);

  const isCollapsed = (key: string, index: number, holdsActive: boolean) =>
    key in collapsedOverride ? collapsedOverride[key] : index !== 0 && !holdsActive;

  const toggle = (key: string, collapsed: boolean) => {
    setCollapsedOverride((prev) => {
      const next = { ...prev, [key]: !collapsed };
      try {
        window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(next));
      } catch {
        // Not persisted this time; the toggle still applies in memory.
      }
      return next;
    });
  };

  return (
    <>
      {groups.map((group, index) => {
        const holdsActive = group.items.some((i) => isNavActive(i.href, pathname));
        const collapsed = isCollapsed(group.key, index, holdsActive);
        const badgeTotal = group.items.reduce((sum, e) => sum + (e.badge ?? 0), 0);
        return (
          <div key={group.key} className="pt-3">
            <button
              type="button"
              onClick={() => toggle(group.key, collapsed)}
              aria-expanded={!collapsed}
              className="w-full flex items-center gap-1.5 px-3 py-1.5 text-fg-subtle hover:text-fg transition-colors cursor-pointer"
            >
              <Icon
                name="chevron-right"
                size="w-3 h-3"
                className={`transition-transform ${collapsed ? "" : "rotate-90"}`}
              />
              <span className="text-[11px] font-medium uppercase tracking-wide flex-1 text-left">
                {group.label}
              </span>
              {collapsed && badgeTotal > 0 && <Badge count={badgeTotal} />}
            </button>
            {!collapsed && (
              <div className="space-y-0.5 mt-0.5">
                {group.items.map((item) => (
                  <NavRow
                    key={item.href}
                    href={item.href}
                    onClick={onNavigate}
                    label={item.label}
                    icon={item.icon}
                    description={item.description}
                    active={isNavActive(item.href, pathname)}
                    badge={item.badge}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

function RecentChats({
  recent,
  hasAny,
  activeSessionId,
  allChatsActive,
  onSelect,
  onNavigate,
}: {
  recent: ReturnType<typeof recentWebChats>;
  hasAny: boolean;
  activeSessionId?: string;
  allChatsActive: boolean;
  /** Present on the chat home; elsewhere rows link to `/?session=<id>`. */
  onSelect?: (sessionId: string) => void;
  onNavigate: () => void;
}) {
  if (!hasAny) return null;
  return (
    <div className="pt-3">
      <div className="flex items-center justify-between px-3 py-1.5">
        <span className="text-[11px] font-medium uppercase tracking-wide text-fg-subtle">
          Recent
        </span>
        <Link
          href="/chats"
          onClick={onNavigate}
          title="Every conversation, searchable — including Slack, Telegram and Discord"
          className={`text-[11px] transition-colors ${
            allChatsActive ? "text-fg font-medium" : "text-fg-subtle hover:text-fg"
          }`}
        >
          All chats
        </Link>
      </div>
      {recent.length === 0 ? (
        <p className="px-3 py-1.5 text-xs text-fg-subtle">No web chats yet.</p>
      ) : (
        <div className="space-y-0.5">
          {recent.map((s) => {
            const active = activeSessionId === s.session_id;
            const when = formatRelativeTime(s.updated_at);
            return (
              <SidebarEntry
                key={s.session_id}
                href={onSelect ? undefined : `/?session=${encodeURIComponent(s.session_id)}`}
                onClick={() => {
                  onSelect?.(s.session_id);
                  onNavigate();
                }}
                title={when ? `${sessionTitle(s)} · ${when}` : sessionTitle(s)}
                ariaCurrent={active}
                className={`w-full block text-left px-3 py-2 rounded-lg text-xs truncate transition-colors cursor-pointer ${
                  active
                    ? "bg-surface-overlay text-fg"
                    : "text-fg-muted hover:bg-surface-overlay/60 hover:text-fg"
                }`}
              >
                {sessionTitle(s)}
              </SidebarEntry>
            );
          })}
        </div>
      )}
    </div>
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
  accent,
  badge,
}: {
  href?: string;
  onClick: () => void;
  label: string;
  icon: IconName;
  /** Tooltip explaining the destination — shown via `title` on hover. */
  description?: string;
  active: boolean;
  /** Tints the primary action (New chat). */
  accent?: boolean;
  badge?: number;
}) {
  const tone = active
    ? accent
      ? "bg-indigo-500/15 text-indigo-200 font-medium"
      : "bg-surface-overlay text-fg font-medium"
    : accent
      ? "text-indigo-300 hover:text-indigo-200 hover:bg-surface-overlay"
      : "text-fg-muted hover:text-fg hover:bg-surface-overlay";
  return (
    <SidebarEntry
      href={href}
      onClick={onClick}
      title={description}
      ariaCurrent={active}
      className={`w-full text-left px-3 py-2 min-h-touch rounded-lg flex items-center gap-2.5 text-sm transition-colors cursor-pointer ${tone}`}
    >
      <Icon name={icon} size="w-4 h-4" />
      <span className="flex-1 truncate">{label}</span>
      {badge != null && badge > 0 && <Badge count={badge} />}
    </SidebarEntry>
  );
}

function Badge({ count }: { count: number }) {
  return (
    <span className="text-[10px] bg-amber-500/20 text-amber-400 border border-amber-500/30 rounded-full px-1.5 py-0.5 leading-none font-medium">
      {count}
    </span>
  );
}
