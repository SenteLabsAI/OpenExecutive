"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import {
  getOutsideContent,
  setOutsideContent,
  type OutsideContentSetting,
} from "@/lib/api";
import {
  announceApprovals,
  APPROVALS_LEVELS,
  onApprovalsChanged,
  approvalsTitle,
  type ApprovalsLevel,
} from "@/lib/approvalsLevel";
import { APPROVALS_HREF } from "@/lib/reach";

const DOT: Record<ApprovalsLevel, string> = {
  ask: "bg-emerald-500",
  outsiders: "bg-accent-strong",
  trust: "bg-amber-500",
};

// Sidebar: the Approvals level, one tap from any page. Owner only (the same
// can_change the Delegate card reads); hidden when it can't load.
export default function ApprovalsChip({ onNavigate }: { onNavigate: () => void }) {
  const [setting, setSetting] = useState<OutsideContentSetting | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Bumped whenever a save sets the level, so a load that started before it
  // can't put the older level back.
  const saves = useRef(0);
  const saved = (s: OutsideContentSetting) => {
    saves.current += 1;
    setSetting(s);
  };

  // Loaded once (a failed first load hides the chip), and again on each open
  // (a failed refresh keeps what it shows). The Delegate card announces its
  // saves.
  const load = (first: boolean, signal?: AbortSignal) => {
    const at = saves.current;
    getOutsideContent(signal).then(
      (s) => {
        if (saves.current === at) setSetting(s);
      },
      () => {
        if (first && !signal?.aborted) setSetting(null);
      },
    );
  };

  useEffect(() => {
    const ctrl = new AbortController();
    load(true, ctrl.signal);
    const stop = onApprovalsChanged<OutsideContentSetting>(saved);
    return () => {
      ctrl.abort();
      stop();
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!setting || !setting.can_change) return null;

  const level = setting.mode as ApprovalsLevel;

  const pick = async (value: ApprovalsLevel) => {
    if (busy || value === level) return;
    setBusy(true);
    setError(null);
    try {
      const next = await setOutsideContent(value);
      saved(next);
      announceApprovals(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const go = () => {
    setOpen(false);
    onNavigate();
  };

  return (
    <div ref={box} className="relative mb-1">
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          // Opening looks again: it may have changed from email or a chat app.
          if (!open) load(false);
          setOpen(!open);
        }}
        className="w-full flex items-center gap-2.5 rounded-xl border border-line bg-surface px-3 py-2 text-left hover:bg-surface-overlay transition-colors cursor-pointer"
      >
        <span aria-hidden="true" className={`h-2 w-2 flex-shrink-0 rounded-full ${DOT[level] ?? DOT.ask}`} />
        <span className="min-w-0 flex-1">
          <span className="block text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">Approvals</span>
          <span className="block truncate text-sm font-semibold text-fg">{approvalsTitle(level)}</span>
        </span>
        <span className="text-xs font-medium text-accent">Change</span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Approvals"
          className="absolute bottom-full left-0 z-50 mb-2 w-[min(19rem,calc(100vw-1.5rem))] rounded-2xl border border-line bg-surface-elevated p-2 shadow-xl"
        >
          <p className="px-3 pt-2 pb-1 text-sm font-semibold text-fg">Approvals</p>
          <div role="radiogroup" aria-label="Approvals" className="flex flex-col gap-1">
            {APPROVALS_LEVELS.map((o) => {
              const picked = o.value === level;
              return (
                <button
                  key={o.value}
                  type="button"
                  role="radio"
                  aria-checked={picked}
                  disabled={busy}
                  onClick={() => void pick(o.value)}
                  className={`rounded-xl px-3 py-2 text-left transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                    picked ? "bg-accent/10 ring-1 ring-accent-strong" : "hover:bg-surface-overlay"
                  }`}
                >
                  <span className="flex items-center gap-2 text-sm font-semibold text-fg">
                    <span aria-hidden="true" className={`h-2 w-2 rounded-full ${DOT[o.value]}`} />
                    {o.title}
                    {picked && <Icon name="check" size="w-4 h-4" />}
                  </span>
                  <span className="mt-0.5 block text-[13px] text-fg-muted">{o.text}</span>
                </button>
              );
            })}
          </div>


          {error && (
            <p role="alert" className="px-3 pt-2 text-[13px] text-red-600 dark:text-red-400">
              {error}
            </p>
          )}

          <p className="mt-2 border-t border-line px-3 pt-2 pb-1 text-[13px] text-fg-muted">
            Works the same in the app, email and chat apps.{" "}
            <Link href={APPROVALS_HREF} onClick={go} className="font-medium text-accent hover:underline">
              More on Delegate
            </Link>
          </p>
        </div>
      )}
    </div>
  );
}
