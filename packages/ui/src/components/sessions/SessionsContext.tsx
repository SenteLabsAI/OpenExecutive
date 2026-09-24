"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { deleteSession, listSessions, type SessionSummary } from "@/lib/api";

// App-wide conversation list. Lives in the root layout so the shared sidebar
// shows the same Recent list on every route, and the chat home can refresh it
// after a turn without owning the state itself.
interface SessionsContextValue {
  sessions: SessionSummary[];
  /** False until the first list request has settled, so pages can tell "empty" from "not loaded yet". */
  loaded: boolean;
  /** True when the latest list request failed — distinct from an empty history. */
  error: boolean;
  refresh: () => void;
  /** Confirms, deletes, and refreshes. Resolves true only if the chat was deleted. */
  remove: (sessionId: string) => Promise<boolean>;
}

const SessionsContext = createContext<SessionsContextValue | null>(null);

export function SessionsProvider({ children }: { children: React.ReactNode }) {
  const { status } = useSession();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);
  // Only the newest request may write: several refreshes can overlap (mount,
  // /chats, after a delete), and an older response landing last would
  // resurrect a just-deleted chat.
  const requestSeqRef = useRef(0);

  const refresh = useCallback(() => {
    const seq = ++requestSeqRef.current;
    listSessions()
      .then((list) => {
        if (seq !== requestSeqRef.current) return;
        setSessions(list);
        setError(false);
        setLoaded(true);
      })
      .catch(() => {
        if (seq !== requestSeqRef.current) return;
        setError(true);
        setLoaded(true);
      });
  }, []);

  // Only fetch once signed in — /signin renders under this provider too.
  useEffect(() => {
    if (status === "authenticated") refresh();
  }, [status, refresh]);

  const remove = useCallback(
    async (sessionId: string) => {
      if (!window.confirm("Delete this chat? This cannot be undone.")) return false;
      try {
        await deleteSession(sessionId);
      } catch (err) {
        console.error(err);
        window.alert("Failed to delete chat.");
        return false;
      }
      refresh();
      return true;
    },
    [refresh],
  );

  const value = useMemo(
    () => ({ sessions, loaded, error, refresh, remove }),
    [sessions, loaded, error, refresh, remove],
  );
  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>;
}

export function useSessions(): SessionsContextValue {
  const ctx = useContext(SessionsContext);
  if (!ctx) throw new Error("useSessions must be used inside <SessionsProvider>");
  return ctx;
}
