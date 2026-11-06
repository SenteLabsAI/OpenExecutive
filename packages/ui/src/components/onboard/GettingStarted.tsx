"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSessions } from "@/components/sessions/SessionsContext";
import { buttonClass } from "@/components/ui/Button";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { getPeopleViewer, getSetupStatus, listPeople } from "@/lib/api";
import {
  checklistVisible,
  checklistProgress,
  gettingStartedItems,
  opensInPlace,
  recall,
  remember,
  STORAGE_KEYS,
  viewerIsOwner,
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
  // Whether the viewer is the owner: a teammate sees only their own rows.
  const [viewerIsPrincipal, setViewerIsPrincipal] = useState<boolean | undefined>(undefined);
  const [rosterHasPrincipal, setRosterHasPrincipal] = useState<boolean | undefined>(undefined);
  // Read once, on the client: the card only renders after /health answers.
  const [hidden, setHidden] = useState<boolean>(() => recall(STORAGE_KEYS.checklistHidden) === "1");

  useEffect(() => {
    if (hidden || profileLoaded === undefined) return;
    const ctrl = new AbortController();
    getSetupStatus(ctrl.signal)
      .then((s) => setChecks(s.checks))
      .catch(() => setChecks([]));
    listPeople()
      .then((ps) => {
        setTeamSize(ps.length);
        setRosterHasPrincipal(ps.some((p) => p.is_principal));
      })
      .catch(() => undefined)
      .finally(() => setPeopleLoaded(true));
    // Unknown viewer (the lookup failed): the card stays unshown rather
    // than guess whose rows to show.
    getPeopleViewer()
      .then((v) => {
        if (!ctrl.signal.aborted) setViewerIsPrincipal(v.is_principal);
      })
      .catch(() => undefined);
    return () => ctrl.abort();
  }, [hidden, profileLoaded]);

  const isPrincipal = viewerIsOwner(viewerIsPrincipal, rosterHasPrincipal);
  const items = useMemo(
    () =>
      gettingStartedItems({
        profileLoaded: profileLoaded === true,
        companyName,
        hasAsked: sessionsLoaded && sessions.length > 0,
        checks,
        team: mode === "team",
        teamSize,
        isPrincipal,
      }),
    [profileLoaded, companyName, sessionsLoaded, sessions.length, checks, mode, teamSize, isPrincipal],
  );
  const hide = useCallback(() => {
    remember(STORAGE_KEYS.checklistHidden, "1");
    setHidden(true);
  }, []);

  const visible = checklistVisible({
    hidden,
    loaded:
      profileLoaded !== undefined &&
      checks !== null &&
      sessionsLoaded &&
      peopleLoaded &&
      isPrincipal !== undefined,
    items,
  });
  return { items, visible, hide };
}

// `onAsk` starts the chat in place: Home is the page the Ask row links to,
// and a link to the page already open does not remount it, so the link
// alone did nothing there.
export function GettingStartedCard({
  items,
  hide,
  onAsk,
}: GettingStartedState & { onAsk?: () => void }) {
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
            {opensInPlace(item, Boolean(onAsk)) ? (
              <button
                type="button"
                onClick={() => onAsk?.()}
                className={buttonClass(item.optional ? "secondary" : "primary", "sm", "flex-shrink-0")}
              >
                {item.action}
              </button>
            ) : !item.done && (
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
