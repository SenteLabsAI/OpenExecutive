"use client";

import { signIn } from "next-auth/react";
import { useEffect } from "react";

// Rendered by /signin only when the AUTH_DEV_BYPASS provider is active (see
// DEV_BYPASS_EMAIL in auth.ts). Kicks off the credentials flow on mount so
// the sign-in page is never seen during local dev. The page skips this
// component when `?error=` is present, so a denied dev email shows the
// error instead of looping.
export default function DevAutoSignIn({ email, redirectTo }: { email: string; redirectTo: string }) {
  useEffect(() => {
    void signIn("dev-bypass", { redirectTo });
  }, [redirectTo]);

  return (
    <p className="mt-4 text-sm text-fg-muted">
      Signing in as {email} (AUTH_DEV_BYPASS)…
    </p>
  );
}
