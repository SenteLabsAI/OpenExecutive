"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getPeopleViewer } from "@/lib/api";
import { recall, shouldRedirectToOnboard, STORAGE_KEYS } from "@/lib/firstRun";

// Sends the owner to /onboard while no company profile exists. Pages pass
// what /health told them (undefined while loading); the hook finds out
// whether the viewer is the owner. The rule itself is
// shouldRedirectToOnboard in lib/firstRun.ts. Returns true once a redirect
// is under way, so the page can hold its own content.
export function useFirstRunRedirect(profileLoaded: boolean | undefined): boolean {
  const router = useRouter();
  const [isPrincipal, setIsPrincipal] = useState<boolean | undefined>(undefined);

  useEffect(() => {
    // Nothing to decide yet, or nothing to do: don't ask who the viewer is.
    if (profileLoaded !== false) return;
    let cancelled = false;
    getPeopleViewer()
      .then((v) => {
        if (!cancelled) setIsPrincipal(v.is_principal);
      })
      // Unknown viewer: stay put. The banner still links to setup.
      .catch(() => {
        if (!cancelled) setIsPrincipal(false);
      });
    return () => {
      cancelled = true;
    };
  }, [profileLoaded]);

  // Derived, not stored: the same inputs give the same answer on every render.
  const redirecting = shouldRedirectToOnboard({
    profileLoaded,
    isPrincipal,
    later: recall(STORAGE_KEYS.later) === "1",
  });

  useEffect(() => {
    if (redirecting) router.replace("/onboard");
  }, [redirecting, router]);

  return redirecting;
}
