"use client";

import { signOut, useSession } from "next-auth/react";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";

import { useEmbed } from "@/components/embed/EmbedProvider";
import PausedBanner from "@/components/executive/PausedBanner";
import type { Hub } from "@/components/shell/navConfig";
import { usePageCrumbLabel } from "@/components/shell/PageCrumb";
import HubTabs from "@/components/ui/HubTabs";
import { embedRequested, isNativePath, openMessage, queryWithoutEmbed, routeMessage } from "@/lib/embed";

// The page in a host's frame (embedded mode, lib/embed.ts): no sidebar, top
// bar, bottom bar or Ask OE panel (the host has its own), but the hub's tabs,
// the paused banner and the page itself. It tells the host where it is on
// every navigation, keeps ?embed=1 in the address so a reload stays
// embedded, and hands a place the host shows natively to the host. When the
// host's user isn't who is signed in here, it says so.

function postToHost(message: unknown, origin: string | null): void {
  if (origin && message) window.parent.postMessage(message, origin);
}

export default function EmbeddedShell({
  pathname,
  title,
  hub,
  bare,
  children,
}: {
  pathname: string;
  title: string;
  hub: Hub | null;
  // A page that owns its whole layout (sign-in, setup): no tabs or banner.
  bare: boolean;
  children: React.ReactNode;
}) {
  const { init, hostOrigin } = useEmbed();
  const search = useSearchParams()?.toString() ?? "";
  const query = queryWithoutEmbed(search ? `?${search}` : "");
  const crumb = usePageCrumbLabel(pathname);
  const native = init !== null && isNativePath(pathname, init.nativePaths);
  const lastSent = useRef("");

  useEffect(() => {
    if (!embedRequested(window.location.search)) {
      const url = new URL(window.location.href);
      url.searchParams.set("embed", "1");
      window.history.replaceState(window.history.state, "", url);
    }
  }, [pathname, search]);

  useEffect(() => {
    const message = native
      ? openMessage({ kind: "native", path: `${pathname}${query}` })
      : routeMessage(pathname, query, crumb ?? title);
    const key = JSON.stringify(message);
    if (key === lastSent.current) return;
    lastSent.current = key;
    postToHost(message, hostOrigin);
  }, [native, pathname, query, crumb, title, hostOrigin]);

  if (native) {
    return (
      <div className="flex h-full items-center justify-center bg-surface text-sm text-fg-muted">
        Opening in {init?.productName}…
      </div>
    );
  }
  if (bare) return <>{children}</>;
  return (
    <div className="flex h-full flex-col bg-surface text-fg">
      <UserHintWarning />
      <PausedBanner />
      {hub && <HubTabs hub={hub} pathname={pathname} />}
      <div className="flex-1 min-h-0 flex flex-col overflow-y-auto">{children}</div>
    </div>
  );
}

// The host says who is using it (`userHint`, display only). When that isn't
// who is signed in here, say so: the next person at a shared computer must
// not work on as the last one without knowing.
function UserHintWarning() {
  const { init } = useEmbed();
  const { data } = useSession();
  const signedIn = data?.user?.email?.toLowerCase() ?? "";
  const hint = init?.userHint ?? "";
  if (!hint || !signedIn || hint === signedIn) return null;
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm text-fg"
    >
      <span>
        You are signed in here as <strong>{signedIn}</strong>, but {init?.productName ?? "this page"} is open for{" "}
        <strong>{hint}</strong>.
      </span>
      <button
        type="button"
        onClick={() =>
          void signOut({
            callbackUrl: `/signin?embed=1&callbackUrl=${encodeURIComponent(window.location.pathname + window.location.search)}`,
          })
        }
        className="rounded-lg border border-line px-3 py-1 text-sm font-medium hover:bg-surface-overlay"
      >
        Sign out
      </button>
    </div>
  );
}
