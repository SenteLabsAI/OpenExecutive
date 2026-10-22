"use client";

import { useEffect } from "react";

import { signedInMessage } from "@/lib/embed";

export default function SignedIn({ hostOrigins }: { hostOrigins: string[] }) {
  useEffect(() => {
    const opener = window.opener as Window | null;
    if (!opener) return;
    // Addressed to this app's origin and each host origin, exactly: the
    // browser delivers it only if the opener is one of them. It says only
    // that signing in is done, nothing about who.
    for (const origin of [window.location.origin, ...hostOrigins]) {
      opener.postMessage(signedInMessage(), origin);
    }
    window.close();
  }, [hostOrigins]);

  // Seen only when the browser keeps the window open, or nothing opened it.
  return (
    <main className="min-h-screen flex items-center justify-center px-6">
      <p className="text-sm text-fg-muted">You&rsquo;re signed in. You can close this window.</p>
    </main>
  );
}
