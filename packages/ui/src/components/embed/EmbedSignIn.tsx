"use client";

import { useEffect, useRef, useState } from "react";

import { useEmbed } from "@/components/embed/EmbedProvider";
import { acceptsEvent, isSignedInMessage } from "@/lib/embed";

// /signin in a host's frame (embedded mode): most sign-in providers refuse
// to be shown in a frame, so the page offers one button that opens the
// normal sign-in in a popup. The popup ends on /signin/done, which tells this
// frame (and only this app's origin) and closes; the frame then goes on to
// where it was headed. Outside embedded mode, the page's own forms show.

export const SIGN_IN_POPUP_PATH = `/signin?callbackUrl=${encodeURIComponent("/signin/done")}`;

function withEmbed(path: string): string {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("embed", "1");
  return url.pathname + url.search;
}

export default function EmbedSignIn({ destination, children }: { destination: string; children: React.ReactNode }) {
  const { embedded, init } = useEmbed();
  const popup = useRef<Window | null>(null);
  const [blocked, setBlocked] = useState(false);

  useEffect(() => {
    if (!embedded) return;
    const goOn = () => window.location.assign(withEmbed(destination));
    const onMessage = (event: MessageEvent) => {
      if (acceptsEvent(event, [window.location.origin], popup.current) && isSignedInMessage(event.data)) goOn();
    };
    // A popup whose opener was cut off on the way (a provider's
    // Cross-Origin-Opener-Policy) can't send its message: check the session
    // when the person comes back to this page instead.
    const onFocus = () => {
      if (!popup.current) return;
      fetch("/api/auth/session")
        .then((res) => (res.ok ? res.json() : null))
        .then((session) => {
          if (session?.user) goOn();
        })
        .catch(() => undefined);
    };
    window.addEventListener("message", onMessage);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("focus", onFocus);
    };
  }, [embedded, destination]);

  if (!embedded) return <>{children}</>;
  const open = () => {
    // A name of its own each time: a page that knew the name could take the
    // window over and show its own look-alike sign-in in it.
    const name = `oe-sign-in-${crypto.getRandomValues(new Uint32Array(2)).join("-")}`;
    popup.current = window.open(SIGN_IN_POPUP_PATH, name, "popup,width=480,height=640");
    setBlocked(popup.current === null);
  };
  return (
    <div className="mt-6">
      <button
        type="button"
        onClick={open}
        className="w-full rounded-md bg-white px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-zinc-100 transition"
      >
        Sign in to {init?.productName}
      </button>
      <p className="mt-3 text-xs text-fg-subtle">
        {blocked
          ? "Your browser blocked the sign-in window. Allow pop-ups for this page and try again."
          : "Sign-in opens in a small window, which closes itself when you're done."}
      </p>
    </div>
  );
}

/** This app's name and mark, or in a host's frame the name the host gives it. */
export function EmbedProductName({ children }: { children: React.ReactNode }) {
  const { embedded, init } = useEmbed();
  return <>{embedded && init ? init.productName : children}</>;
}
