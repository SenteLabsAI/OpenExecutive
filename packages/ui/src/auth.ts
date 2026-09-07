import NextAuth from "next-auth";
import Google from "next-auth/providers/google";

// Env-var fallback for the bootstrap case (fresh install, roster still
// empty). Once any Person row exists with an email, the backend's
// /auth/allowed-emails endpoint becomes authoritative and this list is
// only consulted to admit the first operator.
const ALLOWED_EMAILS_FALLBACK: ReadonlySet<string> = new Set(
  (process.env.ALLOWED_EMAILS ?? "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter((e) => e.length > 0),
);

const BACKEND_BASE = process.env.BACKEND_BASE_URL ?? "http://localhost:8000";
const BACKEND_SHARED_SECRET = process.env.BACKEND_SHARED_SECRET ?? "";

// 5-minute cache. Cheap insurance against hammering the backend on every
// sign-in attempt and keeps sign-in latency bounded if the backend is
// momentarily slow. NextAuth's signIn callback is server-side (Node
// runtime) so this module-level cache is per-server-instance.
const ROSTER_TTL_MS = 5 * 60 * 1000;
let rosterCache: { fetchedAt: number; emails: Set<string> } | null = null;

async function fetchRosterEmails(): Promise<Set<string> | null> {
  const now = Date.now();
  if (rosterCache && now - rosterCache.fetchedAt < ROSTER_TTL_MS) {
    return rosterCache.emails;
  }
  try {
    const headers: Record<string, string> = {};
    if (BACKEND_SHARED_SECRET) headers["x-api-key"] = BACKEND_SHARED_SECRET;
    const res = await fetch(`${BACKEND_BASE}/auth/allowed-emails`, {
      headers,
      // Sign-in is rare; don't let stale Next.js fetch caches gate access.
      cache: "no-store",
    });
    if (!res.ok) {
      console.warn(`[auth] roster fetch failed (HTTP ${res.status}); falling back to ALLOWED_EMAILS env`);
      return null;
    }
    const rows = (await res.json()) as Array<{ email: string; person_id: number }>;
    const emails = new Set(rows.map((r) => r.email.toLowerCase()));
    rosterCache = { fetchedAt: now, emails };
    return emails;
  } catch (err) {
    console.warn(`[auth] roster fetch error; falling back to ALLOWED_EMAILS env: ${String(err)}`);
    return null;
  }
}

/**
 * Resolve whether an email is permitted by the current allowlist regime.
 *
 * Returns `{ allowed, source }`. `source` is one of:
 *  - `roster` — the People table is populated and authoritative; matched.
 *  - `env_empty_roster` — roster has no email-bearing rows yet; env fallback used.
 *  - `env_after_fetch_error` — backend fetch failed; env fallback used.
 *
 * Called by both the NextAuth `signIn` callback (strict, denies on miss) and
 * the `authorized` callback (re-runs on every gated request so a user
 * removed from the roster mid-session is bounced on next request).
 */
async function checkEmailAllowed(
  email: string,
): Promise<{ allowed: boolean; source: string }> {
  const roster = await fetchRosterEmails();
  if (roster && roster.size > 0) {
    return { allowed: roster.has(email), source: "roster" };
  }
  return {
    allowed: ALLOWED_EMAILS_FALLBACK.has(email),
    source: roster === null ? "env_after_fetch_error" : "env_empty_roster",
  };
}

// Fire-and-forget audit call to the backend. Never awaited — auth must never
// block or expose errors due to audit failures.
function auditAuth(
  event_type: string,
  summary: string,
  actor: string | null,
  details: Record<string, unknown>,
): void {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (BACKEND_SHARED_SECRET) headers["x-api-key"] = BACKEND_SHARED_SECRET;
  fetch(`${BACKEND_BASE}/audit/log`, {
    method: "POST",
    headers,
    body: JSON.stringify({ event_type, summary, actor, details }),
  }).catch(() => {
    // Intentionally swallowed — audit failures must never surface to users.
  });
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [Google],
  // 24h JWT TTL. Defence in depth alongside the `authorized` re-check
  // below — a session that somehow drifts out of sync with the roster
  // is corrected on next access, but also naturally expires within a
  // day so stale JWTs never coast forever.
  session: { strategy: "jwt", maxAge: 24 * 60 * 60 },
  pages: {
    signIn: "/signin",
    error: "/signin",
  },
  callbacks: {
    // Strict initial gate. Requires `email_verified === true` explicitly: a
    // missing / non-boolean value fails closed. Google always returns true
    // for real accounts.
    signIn: async ({ profile }) => {
      const email = profile?.email?.toLowerCase();
      if (!email) {
        auditAuth("auth_login", "Login denied: no email", null, { denied: true, reason: "no_email" });
        return false;
      }
      if (profile?.email_verified !== true) {
        auditAuth("auth_login", `Login denied: ${email} (email not verified)`, email, { denied: true, reason: "email_not_verified" });
        return false;
      }
      const { allowed, source } = await checkEmailAllowed(email);
      if (!allowed) {
        auditAuth(
          "auth_login",
          `Login denied: ${email} (not in ${source})`,
          email,
          { denied: true, reason: "not_in_allowlist", source },
        );
        return false;
      }
      return true;
    },
    // Re-runs on every request gated by the middleware (see middleware.ts).
    // Without this, a user removed from the roster mid-session — or one
    // whose JWT predates the roster being installed — would keep coasting
    // until their JWT expires. Fail-open on roster-fetch errors (signal:
    // source === "env_after_fetch_error") so a brief backend hiccup
    // doesn't lock out everyone with a valid session — the strict
    // `signIn` gate already vetted them once.
    authorized: async ({ auth }) => {
      if (!auth?.user?.email) return false;
      const email = auth.user.email.toLowerCase();
      const { allowed, source } = await checkEmailAllowed(email);
      if (source === "env_after_fetch_error") return true;
      if (!allowed) {
        // Fire-and-forget audit so a mid-session eviction leaves a
        // trail even if the user never re-attempts sign-in.
        auditAuth(
          "auth_logout",
          `Session revoked: ${email} (not in ${source})`,
          email,
          { revoked: true, reason: "not_in_allowlist", source },
        );
      }
      return allowed;
    },
  },
  events: {
    signIn: ({ user }) => {
      const email = user.email?.toLowerCase() ?? null;
      auditAuth("auth_login", `Login: ${email ?? "unknown"}`, email, { provider: "google" });
    },
    signOut: (message) => {
      // JWT strategy sends { token }, session strategy sends { session }.
      const token = "token" in message ? message.token : undefined;
      const email = typeof token?.email === "string" ? token.email.toLowerCase() : null;
      auditAuth("auth_logout", `Logout: ${email ?? "unknown"}`, email, {});
    },
  },
});
