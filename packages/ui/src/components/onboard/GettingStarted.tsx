"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSessions } from "@/components/sessions/SessionsContext";
import { buttonClass } from "@/components/ui/Button";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { getSetupStatus, listPeople } from "@/lib/api";
import {
  checklistVisible,
  checklistProgress,
  gettingStartedItems,
  recall,
  remember,
  STORAGE_KEYS,
  type ChecklistItem,
} from "@/lib/firstRun";
import type { SetupCheck } from "@/lib/setupStatus";

export interface GettingStartedState {
  items: ChecklistItem[];
  /** False once every required row is done, or after Hide. */
  visible: boolean;
  hide: () => void;
}

// Home's banner slot on the first days: what is set up and what is left,
// with a button per row. The rows and their rules are gettingStartedItems in
// lib/firstRun.ts. The hook decides whether there is anything to show (so
// Home can fall back to its usual banner), the card draws it.
export function useGettingStarted(
  profileLoaded: boolean | undefined,
  companyName: string | null | undefined,
): GettingStartedState {
  const { sessions, loaded: sessionsLoaded } = useSessions();
  const { mode } = useWorkspace();
  const [checks, setChecks] = useState<readonly SetupCheck[] | null>(null);
  const [teamSize, setTeamSize] = useState<number>(1);
  const [peopleLoaded, setPeopleLoaded] = useState(false);
  // Read once, on the client: the card only renders after /health answers.
  const [hidden, setHidden] = useState<boolean>(() => recall(STORAGE_KEYS.checklistHidden) === "1");

  useEffect(() => {
    if (hidden || profileLoaded === undefined) return;
    const ctrl = new AbortController();
    getSetupStatus(ctrl.signal)
      .then((s) => setChecks(s.checks))
      .catch(() => setChecks([]));
    listPeople()
      .then((ps) => setTeamSize(ps.length))
      .catch(() => undefined)
      .finally(() => setPeopleLoaded(true));
    return () => ctrl.abort();
  }, [hidden, profileLoaded]);

  const items = useMemo(
    () =>
      gettingStartedItems({
        profileLoaded: profileLoaded === true,
        companyName,
        hasAsked: sessionsLoaded && sessions.length > 0,
        checks,
        team: mode === "team",
        teamSize,
      }),
    [profileLoaded, companyName, sessionsLoaded, sessions.length, checks, mode, teamSize],
  );
  const hide = useCallback(() => {
    remember(STORAGE_KEYS.checklistHidden, "1");
    setHidden(true);
  }, []);

  const visible = checklistVisible({
    hidden,
    loaded: profileLoaded !== undefined && checks !== null && sessionsLoaded && peopleLoaded,
    items,
  });
  return { items, visible, hide };
}

export function GettingStartedCard({ items, hide }: GettingStartedState) {
  const progress = checklistProgress(items);
  return (
    <section
      aria-label="Getting started"
      className="rounded-2xl border border-line bg-surface-elevated px-4 pb-1 pt-3"
    >
      <div className="flex items-center justify-between gap-3 pb-1">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-fg-subtle">
          Getting started · {progress.done} of {progress.total}
        </h2>
        <button
          type="button"
          onClick={hide}
          className="text-xs text-fg-muted hover:text-fg cursor-pointer"
        >
          Hide
        </button>
      </div>
      <ul>
        {items.map((item) => (
          <li
            key={item.id}
            className="flex items-center gap-3 border-b border-line py-3 last:border-0"
          >
            <span
              aria-hidden
              className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-xs font-bold ${
                item.done ? "bg-emerald-400 text-emerald-950" : "border-2 border-fg-subtle"
              }`}
            >
              {item.done ? "✓" : ""}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[15px] font-semibold text-fg">
                {item.title}
                <span className="sr-only">{item.done ? " (done)" : ""}</span>
              </p>
              <p className="text-[13px] text-fg-muted leading-snug">{item.detail}</p>
            </div>
            {!item.done && (
              <Link
                href={item.href}
                className={buttonClass(item.optional ? "secondary" : "primary", "sm", "flex-shrink-0")}
              >
                {item.action}
              </Link>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
