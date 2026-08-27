"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import BrandMark from "@/components/BrandMark";
import Briefing from "@/components/Briefing";
import Chat from "@/components/Chat";
import DebugPanel from "@/components/DebugPanel";
import Icon from "@/components/Icon";
import RecentSessions from "@/components/RecentSessions";
import SidebarNav from "@/components/SidebarNav";
import { MobileBottomNav } from "@/components/shell/AppShell";
import { buildPrimaryNav, GUIDE_NAV_ITEM, SETTINGS_NAV_ITEM } from "@/components/shell/navConfig";
import UserBadge from "@/components/UserBadge";
import { ChatMessage, DebugEvent, ReviewStats, SessionSummary, deleteSession, getReviewStats, getSessionMessages, listSessions } from "@/lib/api";
import Link from "next/link";
import { useRouter } from "next/navigation";

interface HealthData {
  company_profile_loaded: boolean;
  company_name?: string;
  status: string;
}

export default function HomePage() {
  const { data: session } = useSession();
  const firstName = session?.user?.name?.trim().split(/\s+/)[0];

  const [health, setHealth] = useState<HealthData | null>(null);
  const [debugOpen, setDebugOpen] = useState(false);
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>([]);
  const activeTurnIdRef = useRef<string | null>(null);
  const [isTurnInFlight, setIsTurnInFlight] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | undefined>();
  const [activeMessages, setActiveMessages] = useState<ChatMessage[]>([]);
  const [reviewStats, setReviewStats] = useState<ReviewStats | null>(null);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // Briefing-first landing: default to "briefing" so opening the app shows
  // what's been happening, not an empty chat. Switches to "chat" when the
  // user picks a session, clicks "New chat", or clicks a briefing item to
  // continue the thread in conversation.
  const [mode, setMode] = useState<"briefing" | "chat">("briefing");
  // Seeded into Chat's input when the user enters chat mode from a briefing
  // item. Cleared on every mode transition so it doesn't leak between turns.
  const [pendingPrompt, setPendingPrompt] = useState<string | undefined>(undefined);

  useEffect(() => {
    fetch("/api/backend/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setHealth({ status: "error", company_profile_loaded: false }));
  }, []);

  const refreshSessions = useCallback(() => {
    listSessions()
      .then(setSessions)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    getReviewStats().then(setReviewStats).catch(() => {});
  }, []);

  const handleSelectSession = useCallback(async (sessionId: string) => {
    try {
      const msgs = await getSessionMessages(sessionId);
      setActiveSessionId(sessionId);
      setActiveMessages(msgs);
      setDebugEvents([]);
      setMobileNavOpen(false);
      setMode("chat");
      setPendingPrompt(undefined);
    } catch {
      // ignore — session may not exist yet
    }
  }, []);

  const handleNewChat = useCallback(() => {
    setActiveSessionId(undefined);
    setActiveMessages([]);
    setDebugEvents([]);
    activeTurnIdRef.current = null;
    setIsTurnInFlight(false);
    setMobileNavOpen(false);
    setMode("chat");
    setPendingPrompt(undefined);
  }, []);

  // Continue a briefing thread in chat — invoked when the user clicks a
  // Department card, proposal, or activity row. Switches mode to "chat"
  // and seeds the input with the briefing context. The user can edit
  // before sending, or just hit send.
  const handleContinueFromBriefing = useCallback((prompt: string) => {
    setActiveSessionId(undefined);
    setActiveMessages([]);
    setDebugEvents([]);
    setMode("chat");
    setPendingPrompt(prompt);
  }, []);

  // Reset to the briefing view from anywhere. Used by the sidebar
  // brandmark/header — clicking it returns home from a chat session.
  const handleBackToBriefing = useCallback(() => {
    setActiveSessionId(undefined);
    setActiveMessages([]);
    setDebugEvents([]);
    setMode("briefing");
    setPendingPrompt(undefined);
    setMobileNavOpen(false);
  }, []);

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      if (!window.confirm("Delete this chat? This cannot be undone.")) return;
      try {
        await deleteSession(sessionId);
      } catch (err) {
        console.error(err);
        window.alert("Failed to delete chat.");
        return;
      }
      setActiveSessionId((current) => {
        if (current === sessionId) {
          setActiveMessages([]);
          setDebugEvents([]);
          return undefined;
        }
        return current;
      });
      refreshSessions();
    },
    [refreshSessions]
  );

  const handleTurnComplete = useCallback((sessionId: string) => {
    setActiveSessionId(sessionId);
    setIsTurnInFlight(false);
    refreshSessions();
  }, [refreshSessions]);

  // Cross-route "New chat" entry: the AppShell rail and mobile bottom
  // nav link to `/?new=1` from every inner route. When that param is
  // present on mount, reset to a fresh chat and strip the query so a
  // refresh doesn't reapply the action.
  //
  // Read directly from `window.location` rather than `useSearchParams`:
  // that hook opts the page out of static rendering in Next.js unless
  // wrapped in <Suspense>, and the chat home is a heavy static page we
  // want to keep prerendered. The effect runs client-only anyway.
  const router = useRouter();
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("new") === "1") {
      handleNewChat();
      router.replace("/");
    }
  }, [handleNewChat, router]);

  // Group debug events by turn_id. When we see a new turn_id, reset the
  // panel. Track the current turn_id in a ref — state updater functions
  // must be pure, but React Strict Mode double-invokes them in dev, so
  // doing the "is this a new turn?" check inside `setDebugEvents`'s
  // updater would append the event twice.
  const handleDebugEvent = useCallback((event: DebugEvent) => {
    const incoming = event.turn_id ?? null;
    if (incoming && incoming !== activeTurnIdRef.current) {
      activeTurnIdRef.current = incoming;
      setDebugEvents([event]);
      setIsTurnInFlight(true);
    } else {
      setDebugEvents((prev) => [...prev, event]);
    }
    if (event.kind === "turn_complete" || event.kind === "turn_error") {
      setIsTurnInFlight(false);
    }
  }, []);

  const isOnboarded = health?.company_profile_loaded === true;
  const companyName = health?.company_name;

  const reviewBadge = reviewStats != null ? reviewStats.pending + reviewStats.needs_revision : 0;
  // Primary nav is built from the shared config in
  // `components/shell/navConfig.ts` — the single source of truth the
  // AppShell rail also uses, so the two navs can never drift. Admin /
  // power tools are NOT here; they live on the Settings hub (linked from
  // the footer below). "Today" is intentionally omitted — the Briefing
  // button above is the in-app way back to that content.
  const navSections = buildPrimaryNav({ isOnboarded, reviewBadge });

  return (
    <div className="flex h-full relative">
      {/* Mobile backdrop */}
      {mobileNavOpen && (
        <div
          className="fixed top-8 bottom-0 left-0 right-0 bg-black/50 z-30 md:hidden"
          onClick={() => setMobileNavOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar — slides in on mobile, static on md+ */}
      <aside
        className={`
          fixed top-8 bottom-0 left-0 z-40 w-64 md:w-56 md:top-0 flex-shrink-0
          border-r border-line flex flex-col bg-surface-elevated
          transform transition-transform duration-200
          md:relative md:translate-x-0 md:transition-none
          ${mobileNavOpen ? "translate-x-0" : "-translate-x-full"}
        `}
      >
        {/* Logo — clicking returns to the briefing landing */}
        <div className="px-4 py-5 border-b border-line flex items-center justify-between flex-shrink-0">
          <button
            type="button"
            onClick={handleBackToBriefing}
            aria-label="Back to briefing"
            className="flex items-center gap-2.5 min-w-0 text-left cursor-pointer hover:opacity-80 transition-opacity"
          >
            <div className="flex-shrink-0">
              <BrandMark size="sm" />
            </div>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-fg">Open Executive</p>
              {companyName && (
                <p className="text-xs text-fg-muted truncate">{companyName}</p>
              )}
            </div>
          </button>
          {/* Close button on mobile only */}
          <button
            type="button"
            aria-label="Close menu"
            onClick={() => setMobileNavOpen(false)}
            className="md:hidden min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg cursor-pointer rounded-lg hover:bg-surface-overlay transition-colors"
          >
            <Icon name="close" size="w-5 h-5" />
          </button>
        </div>

        {/* Nav region — own scroll; compresses/scrolls internally only when the
            sidebar is too short, so Recent below always keeps a usable height */}
        <div className="min-h-0 overflow-y-auto pt-3">
        <SidebarNav
          sections={navSections}
          briefingActive={mode === "briefing"}
          newChatActive={mode === "chat" && activeSessionId === undefined}
          onBriefing={handleBackToBriefing}
          onNewChat={handleNewChat}
          onNavigate={() => setMobileNavOpen(false)}
        />

        </div>

        {/* Recent conversations — date-grouped, searchable, own scroll region */}
        <RecentSessions
          sessions={sessions}
          activeSessionId={activeSessionId}
          onSelect={handleSelectSession}
          onDelete={(id) => void handleDeleteSession(id)}
        />

        {/* Spacer — pins the footer to the bottom now that Recent is content-sized */}
        <div className="flex-1 min-h-0" />

        {/* Footer — User Guide (always-visible help) and Settings (the hub
            for admin/power tools), kept out of the primary groups above so
            day-to-day nav stays focused. */}
        <div className="px-2 py-2 border-t border-line flex-shrink-0 space-y-0.5">
          <Link
            href={GUIDE_NAV_ITEM.href}
            onClick={() => setMobileNavOpen(false)}
            title={GUIDE_NAV_ITEM.description}
            className="px-3 py-2.5 min-h-touch rounded-lg hover:bg-surface-overlay text-fg-muted hover:text-fg flex items-center gap-2.5 text-sm transition-colors cursor-pointer"
          >
            <Icon name={GUIDE_NAV_ITEM.icon} size="w-4 h-4" />
            <span className="flex-1">{GUIDE_NAV_ITEM.label}</span>
          </Link>
          <Link
            href={SETTINGS_NAV_ITEM.href}
            onClick={() => setMobileNavOpen(false)}
            title={SETTINGS_NAV_ITEM.description}
            className="px-3 py-2.5 min-h-touch rounded-lg hover:bg-surface-overlay text-fg-muted hover:text-fg flex items-center gap-2.5 text-sm transition-colors cursor-pointer"
          >
            <Icon name={SETTINGS_NAV_ITEM.icon} size="w-4 h-4" />
            <span className="flex-1">{SETTINGS_NAV_ITEM.label}</span>
          </Link>
        </div>

        {/* Signed-in user */}
        <UserBadge variant="sidebar" />
      </aside>

      {/* Main */}
      <main className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <div className="h-14 border-b border-line flex items-center px-4 sm:px-6 flex-shrink-0 justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            {/* Hamburger — mobile only */}
            <button
              type="button"
              aria-label="Open menu"
              aria-expanded={mobileNavOpen}
              onClick={() => setMobileNavOpen(true)}
              className="md:hidden min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg cursor-pointer rounded-lg hover:bg-surface-overlay transition-colors"
            >
              <Icon name="menu" size="w-5 h-5" />
            </button>
            <span className="text-xs text-fg-muted font-medium truncate">
              {isOnboarded && companyName ? `${companyName} · Executive` : "Executive"}
            </span>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              type="button"
              onClick={() => setDebugOpen((o) => !o)}
              className={`flex items-center gap-1.5 text-xs font-medium px-3 py-2 min-h-touch rounded-md transition-colors cursor-pointer ${
                debugOpen
                  ? "bg-violet-500/20 text-violet-300 border border-violet-500/30"
                  : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
              }`}
              aria-label={debugOpen ? "Hide agent activity" : "Show agent activity"}
            >
              <Icon name="activity" size="w-4 h-4" />
              <span className="hidden sm:inline">{debugOpen ? "Hide activity" : "Agent activity"}</span>
            </button>
          </div>
        </div>

        {!isOnboarded && health && (
          <div className="border-b border-line bg-indigo-500/5 px-4 sm:px-6 py-2.5 flex items-center justify-between gap-3">
            <p className="text-xs text-fg-muted">
              No company profile — responses will be generic.
            </p>
            <Link href="/onboard" className="text-xs text-indigo-400 hover:text-indigo-300 font-medium transition-colors whitespace-nowrap cursor-pointer">
              Set up profile →
            </Link>
          </div>
        )}

        <div className="flex-1 min-h-0">
          {mode === "briefing" ? (
            <Briefing
              onContinue={handleContinueFromBriefing}
              showHeader
              firstName={firstName ?? undefined}
            />
          ) : (
            // No `key` here — Chat handles undefined→sid session adoption
            // via its internal `adoptedSessionIdRef` so a just-streamed
            // reply isn't wiped when the parent echoes back the new
            // session id. Mode toggle (briefing ↔ chat) naturally
            // mounts/unmounts via the conditional render above, so
            // `initialInput` is consumed fresh on each chat entry.
            <Chat
              onDebugEvent={handleDebugEvent}
              initialMessages={activeMessages}
              initialSessionId={activeSessionId}
              initialInput={pendingPrompt}
              // Briefing handoffs (Discuss / Approve / Dismiss / Edit&Approve)
              // are the only path that sets pendingPrompt; those are commit-
              // ments, not drafts, so auto-fire the first turn instead of
              // making the user hit Send again.
              autoSubmitInitialInput={Boolean(pendingPrompt)}
              onTurnComplete={handleTurnComplete}
              onTurnStart={() => setIsTurnInFlight(true)}
            />
          )}
        </div>

        {/* Mobile bottom nav — the chat home owns its own layout (it's
            exempt from AppShell), so it renders the shared bar itself to
            match every other route. "More" opens this page's own drawer. */}
        <MobileBottomNav pathname="/" hideFrom="md" onOpenDrawer={() => setMobileNavOpen(true)} />
      </main>

      {/* Debug panel */}
      {debugOpen && (
        <DebugPanel
          events={debugEvents}
          isLive={isTurnInFlight}
          onClose={() => setDebugOpen(false)}
        />
      )}

    </div>
  );
}
