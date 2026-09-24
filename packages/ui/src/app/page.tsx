"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import Briefing from "@/components/Briefing";
import Chat from "@/components/Chat";
import DebugPanel from "@/components/DebugPanel";
import Icon from "@/components/Icon";
import { useSessions } from "@/components/sessions/SessionsContext";
import { MobileBottomNav } from "@/components/shell/AppShell";
import AppSidebar from "@/components/shell/AppSidebar";
import { ChatMessage, DebugEvent, getSessionMessages } from "@/lib/api";
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
  const { sessions, loaded: sessionsLoaded, refresh: refreshSessions } = useSessions();
  const [activeSessionId, setActiveSessionId] = useState<string | undefined>();
  const [activeMessages, setActiveMessages] = useState<ChatMessage[]>([]);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // Briefing-first landing: default to "briefing" so opening the app shows
  // what's been happening, not an empty chat. Switches to "chat" when the
  // user picks a session, clicks "New chat", or clicks a briefing item to
  // continue the thread in conversation.
  const [mode, setMode] = useState<"briefing" | "chat">("briefing");
  // Seeded into Chat's input when the user enters chat mode from a briefing
  // item. Cleared on every mode transition so it doesn't leak between turns.
  const [pendingPrompt, setPendingPrompt] = useState<string | undefined>(undefined);
  // Companion to pendingPrompt: what peer memory records for the handoff turn.
  const [pendingMemoryText, setPendingMemoryText] = useState<string | undefined>(undefined);

  useEffect(() => {
    fetch("/api/backend/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setHealth({ status: "error", company_profile_loaded: false }));
  }, []);

  // Bumped by every navigation handler below. A session load that resolves
  // after the user has already moved on (new chat, briefing, a hand-off, or
  // another session) sees a stale generation and is dropped instead of
  // yanking them into the old chat mid-turn.
  const selectGenRef = useRef(0);

  const handleSelectSession = useCallback(async (sessionId: string) => {
    const gen = ++selectGenRef.current;
    try {
      const msgs = await getSessionMessages(sessionId);
      if (gen !== selectGenRef.current) return;
      setActiveSessionId(sessionId);
      setActiveMessages(msgs);
      setDebugEvents([]);
      setMobileNavOpen(false);
      setMode("chat");
      setPendingPrompt(undefined);
      setPendingMemoryText(undefined);
    } catch {
      // ignore — session may not exist yet
    }
  }, []);

  const handleNewChat = useCallback(() => {
    selectGenRef.current++;
    setActiveSessionId(undefined);
    setActiveMessages([]);
    setDebugEvents([]);
    activeTurnIdRef.current = null;
    setIsTurnInFlight(false);
    setMobileNavOpen(false);
    setMode("chat");
    setPendingPrompt(undefined);
    setPendingMemoryText(undefined);
  }, []);

  // Continue a briefing thread in chat — invoked when the user clicks a
  // Department card, proposal, or activity row. Switches mode to "chat"
  // and seeds the input with the briefing context. The user can edit
  // before sending, or just hit send.
  const handleContinueFromBriefing = useCallback((prompt: string, memoryText?: string) => {
    selectGenRef.current++;
    setActiveSessionId(undefined);
    setActiveMessages([]);
    setDebugEvents([]);
    setMode("chat");
    setPendingPrompt(prompt);
    setPendingMemoryText(memoryText);
  }, []);

  // Reset to the briefing view from anywhere. Used by the sidebar
  // brandmark/header — clicking it returns home from a chat session.
  const handleBackToBriefing = useCallback(() => {
    selectGenRef.current++;
    setActiveSessionId(undefined);
    setActiveMessages([]);
    setDebugEvents([]);
    setMode("briefing");
    setPendingPrompt(undefined);
    setPendingMemoryText(undefined);
    setMobileNavOpen(false);
  }, []);

  const handleTurnComplete = useCallback((sessionId: string) => {
    // Only adopt a real id. A turn that ends without ever learning one (an
    // aborted stream) would otherwise set this to "", which <Chat> reads as
    // "the parent selected a different session" and clears the transcript
    // with — losing the very reply the stop was meant to keep. The in-flight
    // flag is cleared either way, so the Agent Activity panel never sticks.
    if (sessionId) setActiveSessionId(sessionId);
    setIsTurnInFlight(false);
    refreshSessions();
  }, [refreshSessions]);

  // Cross-route entries: from every inner route the sidebar and mobile
  // bottom nav link to `/?new=1` (New chat) and `/?session=<id>` (a
  // Recent chat, or a row on /chats). When either param is present on
  // mount, apply it and strip the query so a refresh doesn't reapply it.
  // A `session` id is held until the caller's own (owner-scoped) session
  // list has loaded, and opened only if that list contains it: the id comes
  // from the URL, and the per-session backend routes don't check ownership,
  // so a crafted link must not open — or send turns into — someone else's
  // conversation. It is also dropped if the user navigates first.
  //
  // Read directly from `window.location` rather than `useSearchParams`:
  // that hook opts the page out of static rendering in Next 15 unless
  // wrapped in <Suspense>, and the chat home is a heavy static page we
  // want to keep prerendered. The effect runs client-only anyway.
  const router = useRouter();
  const deepLinkRef = useRef<{ sessionId: string; gen: number } | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const sessionParam = params.get("session");
    if (params.get("new") === "1") {
      handleNewChat();
      router.replace("/");
    } else if (sessionParam) {
      deepLinkRef.current = { sessionId: sessionParam, gen: selectGenRef.current };
      router.replace("/");
    }
  }, [handleNewChat, router]);

  useEffect(() => {
    const pending = deepLinkRef.current;
    if (!pending || !sessionsLoaded) return;
    deepLinkRef.current = null;
    if (pending.gen !== selectGenRef.current) return;
    if (sessions.some((s) => s.session_id === pending.sessionId)) {
      void handleSelectSession(pending.sessionId);
    }
  }, [sessions, sessionsLoaded, handleSelectSession]);

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

      {/* Sidebar — the same one every route renders (AppShell uses it
          too); slides in on mobile, static on md+. On the home page its
          entries drive this page's in-memory state instead of navigating. */}
      <AppSidebar
        pathname="/"
        open={mobileNavOpen}
        onClose={() => setMobileNavOpen(false)}
        breakpoint="md"
        isOnboarded={health ? isOnboarded : undefined}
        companyName={companyName}
        home={{
          mode,
          activeSessionId,
          onBriefing: handleBackToBriefing,
          onNewChat: handleNewChat,
          onSelectSession: (id) => void handleSelectSession(id),
        }}
      />

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
              initialMemoryText={pendingMemoryText}
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
