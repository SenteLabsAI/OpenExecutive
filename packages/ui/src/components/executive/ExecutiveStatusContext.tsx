"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import {
  getExecutiveStatus,
  pauseExecutive,
  resumeExecutive,
  type ExecutiveStatus,
} from "@/lib/api";

// App-wide pause state for the Executive's autonomous work. Lives in the root
// layout so the sidebar switch, the paused banner, and the Settings card all
// read one value — the chat home (`/`) is outside the shell, so a per-shell
// fetch would miss it.
interface ExecutiveStatusContextValue {
  /** Null until the first status request succeeds (or while signed out). */
  status: ExecutiveStatus | null;
  /** True while a pause/resume request is in flight. */
  busy: boolean;
  error: string | null;
  pause: (reason?: string) => Promise<void>;
  resume: () => Promise<void>;
  refresh: () => void;
}

const ExecutiveStatusContext = createContext<ExecutiveStatusContextValue | null>(null);

// Another tab, the API, or a teammate can flip the switch; poll so every
// open page converges within this window.
const POLL_MS = 30_000;

export function ExecutiveStatusProvider({ children }: { children: React.ReactNode }) {
  const { status: authStatus } = useSession();
  const [status, setStatus] = useState<ExecutiveStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A poll that started before a pause/resume must not overwrite its result.
  const requestSeqRef = useRef(0);

  const refresh = useCallback(() => {
    const seq = ++requestSeqRef.current;
    getExecutiveStatus()
      .then((s) => {
        if (seq === requestSeqRef.current) setStatus(s);
      })
      // Silent: a transient failure keeps the last known state.
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (authStatus !== "authenticated") return;
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [authStatus, refresh]);

  const run = useCallback(async (op: () => Promise<ExecutiveStatus>) => {
    const seq = ++requestSeqRef.current;
    setBusy(true);
    setError(null);
    try {
      const next = await op();
      if (seq === requestSeqRef.current) setStatus(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }, []);

  const pause = useCallback((reason?: string) => run(() => pauseExecutive(reason)), [run]);
  const resume = useCallback(() => run(resumeExecutive), [run]);

  const value = useMemo(
    () => ({ status, busy, error, pause, resume, refresh }),
    [status, busy, error, pause, resume, refresh],
  );
  return (
    <ExecutiveStatusContext.Provider value={value}>{children}</ExecutiveStatusContext.Provider>
  );
}

export function useExecutiveStatus(): ExecutiveStatusContextValue {
  const ctx = useContext(ExecutiveStatusContext);
  if (!ctx) throw new Error("useExecutiveStatus must be used inside <ExecutiveStatusProvider>");
  return ctx;
}

/** "3:42 PM" today, "Mon 3:42 PM" otherwise. */
export function formatPausedAt(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? time : `${d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} ${time}`;
}
