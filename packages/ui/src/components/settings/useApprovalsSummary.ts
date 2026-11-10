"use client";

import { useEffect, useState } from "react";

import { APPROVALS_OFFERS_CHANGED, getApprovalsSummary, type ApprovalsSummary } from "@/lib/api";

// The reader's Approvals summary (GET /approvals/summary): the last 7 days
// and Could go on its own. Cards that mount together (every card on Home)
// share one read; a page opened later reads again. Let it go or Keep asking
// anywhere replaces it everywhere, so an offer answered on one card or on
// the Approvals tab drops from the rest.

const FRESH_MS = 20_000;
let shared: { at: number; read: Promise<ApprovalsSummary | null> } | null = null;

function load(): Promise<ApprovalsSummary | null> {
  if (!shared || Date.now() - shared.at > FRESH_MS) {
    const read = getApprovalsSummary().catch(() => null);
    shared = { at: Date.now(), read };
  }
  return shared.read;
}

export function useApprovalsSummary(): ApprovalsSummary | null {
  const [summary, setSummary] = useState<ApprovalsSummary | null>(null);

  useEffect(() => {
    let live = true;
    void load().then((next) => {
      if (live) setSummary(next);
    });
    const changed = (event: Event) => {
      const next = (event as CustomEvent<ApprovalsSummary>).detail;
      shared = { at: Date.now(), read: Promise.resolve(next) };
      setSummary(next);
    };
    window.addEventListener(APPROVALS_OFFERS_CHANGED, changed);
    return () => {
      live = false;
      window.removeEventListener(APPROVALS_OFFERS_CHANGED, changed);
    };
  }, []);

  return summary;
}
