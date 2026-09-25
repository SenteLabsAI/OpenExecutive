"use client";

import { signOut, useSession } from "next-auth/react";

interface UserBadgeProps {
  /** "sidebar" (vertical, with sign-out below name) or "compact" (single row, name only). */
  variant?: "sidebar" | "compact";
}

// Displays the signed-in Google user's name + avatar. Click → sign out.
//
// Why visible: prior to this, the signed-in identity was invisible in the
// UI. The /chat route already attaches the session email as
// `x-caller-email` so the backend can resolve the caller to a Person row
// — but a user looking at the screen had no way to confirm WHICH account
// they were signed in as. Especially relevant when multiple operators
// share a deployment.
//
export default function UserBadge({ variant = "compact" }: UserBadgeProps) {
  const { data: session, status } = useSession();

  if (status === "loading") {
    return (
      <div className="flex items-center gap-2 text-xs text-fg-subtle">
        <div className="w-6 h-6 rounded-full bg-surface-overlay animate-pulse" />
        <span className="hidden sm:inline">Loading…</span>
      </div>
    );
  }

  // Local login: no Google account and nothing to sign out of — the
  // sign-in page would just offer "Open" again.
  if (session?.localLogin) {
    return (
      <div
        className={
          variant === "sidebar"
            ? "px-3 py-3 border-t border-line flex items-center gap-2.5 flex-shrink-0"
            : "flex items-center gap-2 text-xs text-fg-muted"
        }
        title="Local login: only this computer can reach Open Executive"
      >
        <Avatar name="ME" size={variant === "sidebar" ? "w-7 h-7" : "w-6 h-6"} />
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium text-fg truncate">You (owner)</p>
          {variant === "sidebar" && (
            <p className="text-[10px] text-fg-muted truncate">On this computer</p>
          )}
        </div>
      </div>
    );
  }

  const user = session?.user;
  if (!user?.email) {
    return null; // shouldn't happen post-middleware, but fail quiet
  }

  const name = user.name || user.email;
  const initials =
    (user.name || user.email)
      .split(/[\s@]/)[0]
      .slice(0, 2)
      .toUpperCase() || "?";

  if (variant === "sidebar") {
    return (
      <div className="px-3 py-3 border-t border-line flex items-center gap-2.5 flex-shrink-0">
        <Avatar name={initials} image={user.image} />
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium text-fg truncate">{name}</p>
          <p className="text-[10px] text-fg-muted truncate">{user.email}</p>
        </div>
        <button
          type="button"
          onClick={() => signOut({ callbackUrl: "/signin" })}
          className="text-[10px] text-fg-muted hover:text-fg transition-colors cursor-pointer flex-shrink-0"
          title="Sign out"
        >
          Sign out
        </button>
      </div>
    );
  }

  // compact (default) — for PageHeader right side
  return (
    <div className="flex items-center gap-2 text-xs text-fg-muted">
      <Avatar name={initials} image={user.image} size="w-6 h-6" />
      <span className="hidden sm:inline truncate max-w-[160px]">{name}</span>
      <button
        type="button"
        onClick={() => signOut({ callbackUrl: "/signin" })}
        className="text-[10px] text-fg-muted hover:text-fg transition-colors cursor-pointer whitespace-nowrap"
        title={`Sign out ${user.email}`}
      >
        Sign out
      </button>
    </div>
  );
}

function Avatar({
  name,
  image,
  size = "w-7 h-7",
}: {
  name: string;
  image?: string | null;
  size?: string;
}) {
  if (image) {
    // Google profile photo. `referrerPolicy="no-referrer"` prevents the
    // Referer header from leaking the app URL to Google's CDN.
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={image}
        alt=""
        referrerPolicy="no-referrer"
        className={`${size} rounded-full flex-shrink-0`}
      />
    );
  }
  return (
    <div
      className={`${size} rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center flex-shrink-0`}
    >
      <span className="text-white text-[10px] font-semibold">{name}</span>
    </div>
  );
}
