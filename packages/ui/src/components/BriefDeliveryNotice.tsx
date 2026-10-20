"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getBriefDelivery, type BriefDeliveryNotice as Notice } from "@/lib/api";

/**
 * The latest morning brief or end-of-day digest that wasn't sent, why, and
 * what fixes it. The API only tells the owner, and says nothing while
 * briefs are going out, so most of the time this is null.
 */
export function useBriefDeliveryNotice(): Notice | null {
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getBriefDelivery(controller.signal)
      .then(setNotice)
      .catch(() => { /* no notice is better than a broken one */ });
    return () => controller.abort();
  }, []);

  return notice;
}

/** Home's banner for a brief that wasn't sent (see useBriefDeliveryNotice). */
export default function BriefDeliveryNotice({ notice }: { notice: Notice }) {
  const day = new Date(notice.at);
  const when = Number.isNaN(day.getTime())
    ? ""
    : ` (${day.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })})`;
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-2xl border border-amber-500/30 bg-amber-500/5 px-4 py-3"
    >
      {/* Themed text with an amber marker: amber text is too faint on the light theme. */}
      <span className="mt-2 inline-block h-2 w-2 flex-shrink-0 rounded-full bg-amber-400" aria-hidden="true" />
      <p className="min-w-0 flex-1 text-[15px] leading-snug text-fg-muted">
        <span className="font-semibold text-fg">
          Your {notice.brief}
          {when} wasn&apos;t sent:
        </span>{" "}
        {notice.problem}. {notice.fix}{" "}
        {notice.readable ? (
          <>
            <Link href="/artifacts" className="font-medium text-accent hover:underline whitespace-nowrap">
              Read it
            </Link>
            <span aria-hidden="true"> · </span>
          </>
        ) : null}
        <Link href="/settings/status" className="font-medium text-accent hover:underline whitespace-nowrap">
          Setup status
        </Link>
      </p>
    </div>
  );
}
