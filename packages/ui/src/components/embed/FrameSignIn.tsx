"use client";

import { useEffect, useState } from "react";

// /signin in any frame (a host origin's, lib/hostOrigins.ts): most sign-in
// providers refuse to be shown in a frame, and a sign-in button under a page
// this app doesn't control is one a lured click can press. So in a frame the
// page shows no sign-in forms, only a link that opens the sign-in page in a
// tab of its own. When the person comes back to the frame signed in, it goes
// on to where it was headed. Outside a frame, the page's own forms show,
// once the browser has said it isn't one: never rendered into a frame first.

export default function FrameSignIn({ destination, children }: { destination: string; children: React.ReactNode }) {
  // null until the browser says whether this is a frame.
  const [framed, setFramed] = useState<boolean | null>(null);

  useEffect(() => {
    const inFrame = window.top !== window.self;
    setFramed(inFrame);
    if (!inFrame) return;
    const onFocus = () => {
      fetch("/api/auth/session")
        .then((res) => (res.ok ? res.json() : null))
        .then((session) => {
          if (session?.user) window.location.assign(destination);
        })
        .catch(() => undefined);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [destination]);

  if (framed === null) return null;
  if (!framed) return <>{children}</>;
  return (
    <div className="mt-6">
      <a
        href={`/signin?callbackUrl=${encodeURIComponent(destination)}`}
        target="_blank"
        rel="noopener"
        className="block w-full rounded-md bg-white px-4 py-2 text-center text-sm font-medium text-zinc-900 hover:bg-zinc-100 transition"
      >
        Sign in to Open Executive
      </a>
      <p className="mt-3 text-xs text-fg-subtle">
        Sign-in opens in a new tab. Come back here when you&rsquo;re done.
      </p>
    </div>
  );
}
