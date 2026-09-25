"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { getWorkspace, type WorkspaceMode } from "@/lib/api";

// How this install is used: "solo" (one person, just for themselves — goals
// grouped by area, no team surfaces) or "team" (a company with departments
// and people). Lives in the root layout next to ExecutiveStatusProvider so
// the sidebar, the chat home (outside the shell), onboarding and Settings all
// read one value.
//
// Reads "team" while loading and after an error, so a team install never
// flashes the solo UI; a solo install shows the team UI for the moment the
// first request takes, which only adds entries it then removes.
interface WorkspaceContextValue {
  mode: WorkspaceMode;
  /** The zone the user set, or null when the server default applies. */
  timezone: string | null;
  /** The zone in effect (the user's, else the server default); null until loaded. */
  effectiveTimezone: string | null;
  /** True until the first request settles (either way). */
  loading: boolean;
  /** Re-read the settings, e.g. after Settings or onboarding changed them. */
  refresh: () => Promise<void>;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const { status: authStatus } = useSession();
  const [mode, setMode] = useState<WorkspaceMode>("team");
  const [timezone, setTimezone] = useState<string | null>(null);
  const [effectiveTimezone, setEffectiveTimezone] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Only the latest request may write: a slow first read must not overwrite
  // the answer to a refresh() issued after a change.
  const seqRef = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++seqRef.current;
    try {
      const ws = await getWorkspace();
      if (seq !== seqRef.current) return;
      setMode(ws.mode === "solo" ? "solo" : "team");
      setTimezone(ws.timezone);
      setEffectiveTimezone(ws.effective_timezone);
    } catch {
      // Keep what we have (the "team" default on a first failure).
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (authStatus === "loading") return;
    if (authStatus !== "authenticated") {
      // Signed out (the sign-in page): nothing to read, and nothing waits on it.
      setLoading(false);
      return;
    }
    void refresh();
  }, [authStatus, refresh]);

  const value = useMemo(
    () => ({ mode, timezone, effectiveTimezone, loading, refresh }),
    [mode, timezone, effectiveTimezone, loading, refresh],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return ctx;
}
