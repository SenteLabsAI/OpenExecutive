"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getAlertApproval } from "@/lib/api";
import {
  APPROVAL_POLL_MAX_MS,
  APPROVAL_POLL_MS,
  approvalLine,
  type AlertApproval,
} from "@/lib/approvals";
import { buttonClass } from "../ui/Button";
import { TONE_TEXT } from "./shared";

// One card approved on this page, in "Needs you" while its work runs on the
// server: "Working on it" with a link to the chat doing it, then "Done" or
// "Didn't work" with Try again. The work itself doesn't depend on this row:
// it keeps going if the page closes.
export default function ApprovedRow({
  approval: initial,
  headline,
  onRetry,
  onClose,
}: {
  approval: AlertApproval;
  headline: string;
  onRetry: () => void;
  onClose: () => void;
}) {
  // Rows are keyed by approval id, so a retry (a new approval) is a new row.
  const [approval, setApproval] = useState(initial);

  useEffect(() => {
    if (approval.status !== "running") return;
    const started = Date.now();
    let stopped = false;
    const timer = setInterval(() => {
      if (Date.now() - started > APPROVAL_POLL_MAX_MS) {
        clearInterval(timer);
        return;
      }
      getAlertApproval(approval.id)
        .then((next) => {
          if (!stopped) setApproval(next);
        })
        .catch(() => { /* keep the last state; the next poll tries again */ });
    }, APPROVAL_POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [approval.id, approval.status]);

  const line = approvalLine(approval, headline);
  const toneClass =
    line.tone === "emerald" ? TONE_TEXT.emerald : line.tone === "rose" ? TONE_TEXT.rose : "text-fg-muted";
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl border border-line bg-surface-elevated px-4 py-3"
      role="status"
    >
      <span className={`min-w-0 flex-1 text-[15px] leading-snug ${toneClass}`}>
        {approval.status === "running" && <span className="mr-1.5 inline-block animate-pulse">●</span>}
        {line.text}
      </span>
      <div className="flex items-center gap-1">
        {line.chatHref && line.chatLabel && (
          <Link href={line.chatHref} className="min-h-[40px] rounded-xl px-3 py-2 text-sm font-medium text-accent hover:bg-accent/10">
            {line.chatLabel}
          </Link>
        )}
        {line.canRetry && (
          <button type="button" onClick={onRetry} className={buttonClass("secondary", "sm")}>
            Try again
          </button>
        )}
        {approval.status !== "running" && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Hide"
            className="min-h-[40px] rounded-xl px-3 text-sm text-fg-muted hover:bg-surface-hover"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
