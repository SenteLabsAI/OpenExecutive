"use client";

import { useCallback, useSyncExternalStore } from "react";

import { AS_YOU_ANCHOR, tabFromHash, type DelegateTab } from "@/lib/reach";

// Which Delegate tab is open. A link's #hash picks it (AS_YOU_HREF,
// AS_ME_HREF, APPROVALS_HREF), including a link clicked while the page is
// already open: the app's own links change the address without a
// hashchange, so a click on one is read here too. Picking a tab puts its
// #hash in the address, so a reload or a shared link opens the same tab.

let current: DelegateTab | null = null;
const listeners = new Set<() => void>();

function set(tab: DelegateTab) {
  if (tab === current) return;
  current = tab;
  listeners.forEach((fn) => fn());
}

function fromAddress() {
  set(tabFromHash(window.location.hash));
}

function onClick(event: MouseEvent) {
  const link = (event.target as Element | null)?.closest?.("a[href]");
  if (!link) return;
  const url = new URL((link as HTMLAnchorElement).href, window.location.href);
  if (url.origin === window.location.origin && url.pathname === "/delegate" && url.hash) {
    set(tabFromHash(url.hash));
  }
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  if (listeners.size === 1) {
    current = tabFromHash(window.location.hash);
    window.addEventListener("hashchange", fromAddress);
    window.addEventListener("popstate", fromAddress);
    document.addEventListener("click", onClick, true);
  }
  fn();
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0) {
      window.removeEventListener("hashchange", fromAddress);
      window.removeEventListener("popstate", fromAddress);
      document.removeEventListener("click", onClick, true);
    }
  };
}

function snapshot(): DelegateTab {
  return current ?? tabFromHash(window.location.hash);
}

export function useDelegateTab(): [DelegateTab, (tab: DelegateTab) => void] {
  const tab = useSyncExternalStore<DelegateTab>(subscribe, snapshot, () => AS_YOU_ANCHOR);
  const pick = useCallback((next: DelegateTab) => {
    window.history.replaceState(window.history.state, "", `#${next}`);
    set(next);
  }, []);
  return [tab, pick];
}
