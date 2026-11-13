"use client";

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import { getBrowserStatus } from "@/lib/api";

// The chat's card when the Executive needed your browser and it
// wasn't connected (the stream's `browser_needed` event,
// browser/tools.py `take_needed`). The reply says in a sentence that
// it can do it; the card holds the only steps. It checks every few seconds
// and, once you press Connect, turns into "Connected" with Go ahead, which
// asks it to carry on with what you asked.

export const BROWSER_SETTINGS_HREF = "/settings/browser";
// What Go ahead sends: the turn before has what they asked for.
export const BROWSER_GO_AHEAD = "Go ahead, my browser is connected now.";
const RECHECK_MS = 3000;

export default function BrowserExtensionCard({
  storeUrl,
  onDismiss,
  onGoAhead,
  disabled = false,
}: {
  storeUrl: string | null;
  onDismiss: () => void;
  onGoAhead: () => void;
  disabled?: boolean;
}) {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (connected) return;
    let alive = true;
    const check = () =>
      getBrowserStatus()
        .then((s) => {
          if (alive && s.connected) setConnected(true);
        })
        .catch(() => undefined);
    const timer = window.setInterval(check, RECHECK_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [connected]);

  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5" aria-live="polite">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Your browser</div>
      {connected ? (
        <>
          <p className="mt-1 text-base sm:text-[17px] font-semibold leading-snug text-emerald-700 dark:text-emerald-400">
            ✓ Connected
          </p>
          <p className="mt-1 text-sm text-fg-muted">I&apos;ll pick up where we left off.</p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button variant="primary" disabled={disabled} onClick={onGoAhead}>
              Go ahead
            </Button>
          </div>
        </>
      ) : (
        <>
          <p className="mt-1 text-base sm:text-[17px] font-semibold leading-snug text-fg">Connect your browser</p>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-[15px] text-fg">
            <li>Click the Open Executive icon in Chrome&apos;s toolbar.</li>
            <li>
              Press <strong>Connect</strong>.
            </li>
          </ol>
          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
            <a
              href={storeUrl ?? BROWSER_SETTINGS_HREF}
              target={storeUrl ? "_blank" : undefined}
              rel={storeUrl ? "noopener noreferrer" : undefined}
              className="text-sm font-medium text-accent hover:underline"
            >
              Don&apos;t have it? Add to Chrome
            </a>
            <Button variant="ghost" className="ml-auto" onClick={onDismiss}>
              Not now
            </Button>
          </div>
        </>
      )}
    </article>
  );
}
