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
  /** True when the latest status read failed — the shown state may be stale,
   *  so the UI must not keep claiming "paused" (or "running"). */
  unknown: boolean;
  /** True while a pause/resume request is in flight. */
  busy: boolean;
  error: string | null;
  /** Resolve true on success; on failure `error` is set. */
  pause: (reason?: string) => Promise<boolean>;
  resume: () => Promise<boolean>;
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
  const [unknown, setUnknown] = useState(false);
  // A poll that started before a pause/resume must not overwrite its result
  // (the seq bump in `run` drops it), and no poll starts while one is in
  // flight — its GET could be answered before the POST commits.
  const requestSeqRef = useRef(0);
  const mutatingRef = useRef(false);

  const refresh = useCallback(() => {
    if (mutatingRef.current) return;
    const seq = ++requestSeqRef.current;
    getExecutiveStatus()
      .then((s) => {
        if (seq !== requestSeqRef.current) return;
        setStatus(s);
        setUnknown(false);
      })
      .catch(() => {
        // Only flag once we have shown a state that may now be wrong; before
        // the first success (e.g. signed out) there is nothing to render.
        if (seq === requestSeqRef.current) setUnknown(true);
      });
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

  const run = useCallback(async (op: () => Promise<ExecutiveStatus>): Promise<boolean> => {
    const seq = ++requestSeqRef.current;
    mutatingRef.current = true;
    setBusy(true);
    setError(null);
    let ok = false;
    try {
      const next = await op();
      if (seq === requestSeqRef.current) {
        setStatus(next);
        setUnknown(false);
      }
      ok = true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      mutatingRef.current = false;
      setBusy(false);
    }
    // A failed request also dropped any poll in flight — re-read the truth.
    if (!ok) refresh();
    return ok;
  }, [refresh]);

  const pause = useCallback((reason?: string) => run(() => pauseExecutive(reason)), [run]);
  const resume = useCallback(() => run(resumeExecutive), [run]);

  const value = useMemo(
    () => ({ status, unknown, busy, error, pause, resume, refresh }),
    [status, unknown, busy, error, pause, resume, refresh],
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
