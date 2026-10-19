"use client";

import { useEffect, useSyncExternalStore } from "react";

import Icon from "@/components/Icon";
import WhatItDoes from "@/components/delegate/WhatItDoes";
import WhoSendsCard from "@/components/delegate/WhoSendsCard";

// What it does for you, beside each Delegate tab. On a wide screen it sits
// to the right of the settings, so a change shows there as it's saved;
// closing it is remembered in this browser. On a narrow one it opens as a
// sheet from the bottom. "Compare" holds Who writes and who sends.

const CLOSED_KEY = "oe.whatItDoes.closed";

// Closed, kept in this browser. Read through useSyncExternalStore so the
// server render (open) and the first client render agree.
const listeners = new Set<() => void>();
let sessionClosed = false;

function readClosed(): boolean {
  try {
    return window.localStorage.getItem(CLOSED_KEY) === "1";
  } catch {
    return sessionClosed;
  }
}

function writeClosed(closed: boolean): void {
  try {
    if (closed) window.localStorage.setItem(CLOSED_KEY, "1");
    else window.localStorage.removeItem(CLOSED_KEY);
  } catch {
    /* storage unavailable: lasts until the page reloads */
  }
  sessionClosed = closed;
  listeners.forEach((fn) => fn());
}

export function useWhatItDoesOpen(): [boolean, (open: boolean) => void] {
  const closed = useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    readClosed,
    () => false,
  );
  return [!closed, (open: boolean) => writeClosed(!open)];
}

function Body() {
  return (
    <>
      <WhatItDoes />
      <details className="group mt-5 rounded-xl border border-line">
        <summary className="cursor-pointer list-none px-3.5 py-3 text-sm font-medium text-fg">
          Compare Act as me, Handle it for me and Take the lead
        </summary>
        <div className="px-2 pb-2 [&>section]:mt-0">
          <WhoSendsCard />
        </div>
      </details>
    </>
  );
}

function Header({ onClose, titleId }: { onClose: () => void; titleId?: string }) {
  return (
    <div className="flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <h2 id={titleId} className="text-lg font-semibold text-fg leading-tight">
          What it does for you
        </h2>
        <p className="mt-1 text-sm text-fg-muted">With your settings right now.</p>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="rounded-lg p-1.5 text-fg-muted hover:bg-surface-overlay hover:text-fg"
      >
        <Icon name="close" className="h-5 w-5" />
      </button>
    </div>
  );
}

/** The column to the right of the settings, on a wide screen. */
export function WhatItDoesAside({ onClose }: { onClose: () => void }) {
  return (
    <aside
      aria-label="What it does for you"
      className="hidden lg:flex w-[400px] flex-none flex-col min-h-0 border-l border-line bg-surface-base"
    >
      <div className="px-5 pt-6 pb-3">
        <Header onClose={onClose} />
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-5 pb-10">
        <Body />
      </div>
    </aside>
  );
}

/** The sheet from the bottom, on a narrow screen. */
export function WhatItDoesSheet({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex flex-col justify-end lg:hidden">
      <div aria-hidden="true" className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="what-it-does-title"
        className="relative flex max-h-[85vh] flex-col rounded-t-2xl border-t border-line bg-surface-base shadow-2xl"
        style={{ paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
      >
        <div aria-hidden className="mx-auto mt-2.5 h-1.5 w-10 rounded-full bg-line" />
        <div className="px-4 pt-3 pb-3">
          <Header onClose={onClose} titleId="what-it-does-title" />
        </div>
        <div className="min-h-0 overflow-y-auto px-4 pb-6">
          <Body />
        </div>
      </div>
    </div>
  );
}
