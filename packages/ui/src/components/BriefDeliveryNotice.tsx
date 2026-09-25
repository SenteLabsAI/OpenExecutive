"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getBriefDelivery, type BriefDeliveryNotice as Notice } from "@/lib/api";

/**
 * Above the Briefing: the latest morning brief or end-of-day digest that
 * wasn't sent, why, and what fixes it. The API only tells the owner, and
 * says nothing while briefs are going out, so most of the time this renders
 * nothing.
 */
export default function BriefDeliveryNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getBriefDelivery(controller.signal)
      .then(setNotice)
      .catch(() => { /* no notice is better than a broken one */ });
    return () => controller.abort();
  }, []);

  if (!notice) return null;
  return (
    <div
      role="status"
      className="border-b border-line bg-amber-500/5 px-4 sm:px-6 py-2.5 flex items-start gap-2 flex-shrink-0"
    >
      {/* Themed text with an amber marker: amber text is too faint on the light theme. */}
      <span className="mt-1.5 inline-block w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0" aria-hidden="true" />
      <p className="flex-1 min-w-0 text-xs text-fg-muted leading-snug">
        <span className="font-medium text-fg">Your {notice.brief} wasn&apos;t sent:</span> {notice.problem}.{" "}
        {notice.fix}{" "}
        <Link href="/artifacts" className="text-indigo-400 hover:text-indigo-300 font-medium whitespace-nowrap">
          Read it
        </Link>
        <span aria-hidden="true"> · </span>
        <Link href="/settings/status" className="text-indigo-400 hover:text-indigo-300 font-medium whitespace-nowrap">
          Setup status
        </Link>
      </p>
    </div>
  );
}
