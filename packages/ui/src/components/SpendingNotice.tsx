"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getSpending } from "@/lib/api";
import { spendingNotice } from "@/lib/spending";

/**
 * Above the Briefing: a heads-up for the owner once this month's AI spending
 * nears the monthly limit (see `spendingNotice`). Most of the time it
 * renders nothing.
 */
export default function SpendingNotice() {
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getSpending(controller.signal)
      .then((s) => setText(spendingNotice(s)))
      .catch(() => { /* no notice is better than a broken one */ });
    return () => controller.abort();
  }, []);

  if (!text) return null;
  return (
    <div
      role="status"
      className="border-b border-line bg-amber-500/5 px-4 sm:px-6 py-2.5 flex items-start gap-2 flex-shrink-0"
    >
      {/* Themed text with an amber marker: amber text is too faint on the light theme. */}
      <span className="mt-1.5 inline-block w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0" aria-hidden="true" />
      <p className="flex-1 min-w-0 text-xs text-fg-muted leading-snug">
        {text}{" "}
        <Link href="/settings#monthly-limit" className="text-indigo-400 hover:text-indigo-300 font-medium whitespace-nowrap">
          Change the limit
        </Link>
      </p>
    </div>
  );
}
