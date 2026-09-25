import NextAuth, { type Session } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";
import type { NextRequest } from "next/server";
import {
  createRosterLoader,
  decideSessionAction,
  describeDenial,
  parseAllowedEmails,
  resolveAllowed,
  type AllowDecision,
} from "@/lib/allowlist";
import {
  LOCAL_OWNER_PROVIDER_ID,
  isLoopbackHost,
  localOwnerModeEnabled,
  localOwnerSessionAllowed,
} from "@/lib/localOwner";

// Operator-controlled allowlist, read once at startup (docs/auth.md promises a
// restart is what makes an edit live). ALWAYS honored: the backend People
// roster is ADDITIVE on top of it, never a replacement — see lib/allowlist.ts
// for why (issue #132).
const ENV_ALLOWED = parseAllowedEmails(process.env.ALLOWED_EMAILS);

const BACKEND_BASE = process.env.BACKEND_BASE_URL ?? "http://localhost:8000";
const BACKEND_SHARED_SECRET = process.env.BACKEND_SHARED_SECRET ?? "";

// One-person mode — `make dev` with Google sign-in not set up (see
// lib/localOwner.ts for the whole guard). `process.env.NODE_ENV` is written out
// literally on purpose: Next.js folds it to a build-time constant, so a
// production build (`next build`, the deploy image) compiles the mode out
// whatever its runtime environment says.
export const LOCAL_OWNER_MODE = localOwnerModeEnabled({
  devServer: process.env.NODE_ENV !== "production",
  flag: process.env.OE_LOCAL_OWNER_MODE,
  googleClientId: process.env.AUTH_GOOGLE_ID,
  publicDeployment: process.env.OE_PUBLIC_DEPLOYMENT,
});

// No password: `authorize` admits only a request whose raw Host header is
// loopback. It signs in one fixed user with no email, which the proxy turns
// into "no x-caller-email", i.e. the principal.
const localOwner = Credentials({
  id: LOCAL_OWNER_PROVIDER_ID,
  name: "This computer",
  credentials: {},
  authorize: (_credentials, request) =>
    LOCAL_OWNER_MODE && isLoopbackHost(request.headers.get("host"))
      ? { id: LOCAL_OWNER_PROVIDER_ID }
      : null,
});

// How long a fetched roster is trusted. `authorized` runs on nearly every
// gated request, so this is what keeps one page load from becoming N backend
// calls — and it bounds how long an archived Person stays admitted. Quoted as
// "5 minutes" in docs/auth.md; keep the two in step.
const ROSTER_TTL_MS = 5 * 60 * 1000;

// Give up on a roster fetch well before undici's ~300s default. Concurrent
// callers share one in-flight request, so an unbounded hang would pin them all
// and keep denying new roster-only sign-ins after the backend recovered.
const ROSTER_FETCH_TIMEOUT_MS = 3000;

// Both NextAuth callbacks are server-side (Node runtime), so the loader's
// cache is per-server-instance.
const loadRoster = createRosterLoader({
  baseUrl: BACKEND_BASE,
  sharedSecret: BACKEND_SHARED_SECRET,
  ttlMs: ROSTER_TTL_MS,
  timeoutMs: ROSTER_FETCH_TIMEOUT_MS,
  // Wrapped rather than passed bare: `fetch` must keep its own receiver.
  fetchImpl: (input, init) => fetch(input, init),
  onWarn: (message) => console.warn(message),
});

/**
 * Resolve whether an email is on the allowlist — the union of ALLOWED_EMAILS
 * and the People roster. An env hit short-circuits, so the roster is never
 * fetched for a configured operator.
 *
 * Called by both the NextAuth `signIn` callback (strict, denies on miss) and
 * the `authorized` callback (re-runs on every gated request so a user removed
 * from the roster mid-session is bounced on next request).
 */
const checkEmailAllowed = (email: string) =>
  resolveAllowed(email, ENV_ALLOWED, loadRoster);

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

type SessionVerdict =
  | { kind: "allow" }
  | { kind: "deny" }
  | { kind: "revoke"; email: string; decision: AllowDecision };

/**
 * May this existing session still be used? The per-request re-check behind
 * the `authorized` callback, and what /signin asks before bouncing a visitor
 * onward (bouncing a session the middleware refuses would loop).
 *
 * A one-person-mode session is valid only while that mode is on and the
 * request comes from this machine. A Google session is re-checked against the
 * allow-list: it fails open ONLY when the user is not in ALLOWED_EMAILS and
 * their roster membership is currently unreadable, so a brief backend hiccup
 * doesn't sign out everyone — the strict `signIn` gate already vetted them
 * once. A *definite* miss (both lists readable, neither matched) revokes.
 */
async function judgeSession(session: Session | null, host: string | null): Promise<SessionVerdict> {
  if (session?.localOwner) {
    return localOwnerSessionAllowed(LOCAL_OWNER_MODE, host) ? { kind: "allow" } : { kind: "deny" };
  }
  const email = session?.user?.email?.toLowerCase();
  if (!email) return { kind: "deny" };
  const decision = await checkEmailAllowed(email);
  // Exhaustive on purpose: a `!== "revoke"` test would admit any future
  // SessionAction, i.e. fail open. This way adding one is a build error.
  switch (decideSessionAction(decision)) {
    case "allow":
    case "allow_roster_unknown":
      return { kind: "allow" };
    case "revoke":
      return { kind: "revoke", email, decision };
  }
}

export async function sessionStillAllowed(session: Session | null, host: string | null): Promise<boolean> {
  return (await judgeSession(session, host)).kind === "allow";
}

/**
 * The response a refused request gets: JSON 401 for API routes (a redirect
 * would be followed by fetch() and break the caller), otherwise a redirect to
 * /signin carrying a same-origin path the sign-in page's own check accepts.
 */
function refuse(request: NextRequest): Response {
  if (request.nextUrl.pathname.startsWith("/api/")) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const signInUrl = new URL("/signin", request.nextUrl.origin);
  signInUrl.searchParams.set("callbackUrl", request.nextUrl.pathname + request.nextUrl.search);
  return Response.redirect(signInUrl);
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  // @auth/core auto-detects trustHost via `AUTH_URL ?? AUTH_TRUST_HOST ??
  // VERCEL ?? CF_PAGES ?? NODE_ENV !== "production"` — a chain of `??`
  // (nullish coalescing). This repo's own local-dev default sets AUTH_URL
  // to an EMPTY STRING, which is present-but-not-nullish, so it
  // short-circuits that chain to `false` *before* AUTH_TRUST_HOST or the
  // NODE_ENV fallback are ever consulted — exactly the documented local-dev
  // config (AUTH_TRUST_HOST=true, AUTH_URL blank) breaks sign-in.
  //
  // Reimplemented below with an emptiness test instead of `??`, so a blank
  // AUTH_URL can no longer mask AUTH_TRUST_HOST. Deliberately NOT a
  // hardcoded `true`: that would trust the host on any real deployment
  // that leaves AUTH_URL blank, letting a spoofed X-Forwarded-Host drive
  // the OAuth callback/redirect origin. VERCEL/CF_PAGES/NODE_ENV are also
  // deliberately dropped, not just reordered: this app doesn't target
  // those platforms, and a literal `process.env.NODE_ENV` check gets
  // folded to a build-time constant by Next.js's bundler (verified against
  // the compiled output — even reading it off an intermediate variable
  // didn't survive Turbopack's dead-code elimination), so it can't
  // actually reflect the container's runtime NODE_ENV the way @auth/core's
  // own dynamic property access does. This repo's documented local-dev
  // setup already sets AUTH_TRUST_HOST=true explicitly and never relied on
  // that fallback anyway. A deployment that sets neither AUTH_URL nor
  // AUTH_TRUST_HOST gets `false` here — fail-closed, matching intent.
  trustHost:
    Boolean(process.env.AUTH_URL?.trim()) ||
    process.env.AUTH_TRUST_HOST?.trim().toLowerCase() === "true",
  // Never both: one-person mode is only ever on while Google is not set up.
  providers: LOCAL_OWNER_MODE ? [localOwner] : [Google],
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
    // for real accounts. A one-person-mode sign-in was already vetted by the
    // provider's `authorize` (mode on, loopback Host).
    signIn: async ({ account, profile }) => {
      if (account?.provider === LOCAL_OWNER_PROVIDER_ID) return LOCAL_OWNER_MODE;
      const email = profile?.email?.toLowerCase();
      if (!email) {
        auditAuth("auth_login", "Login denied: no email", null, { denied: true, reason: "no_email" });
        return false;
      }
      if (profile?.email_verified !== true) {
        auditAuth("auth_login", `Login denied: ${email} (email not verified)`, email, { denied: true, reason: "email_not_verified" });
        return false;
      }
      const { allowed, source, rosterUnknown } = await checkEmailAllowed(email);
      if (!allowed) {
        auditAuth(
          "auth_login",
          `Login denied: ${email} (${describeDenial(source)})`,
          email,
          {
            denied: true,
            reason: "not_in_allowlist",
            source,
            roster_unavailable: rosterUnknown,
          },
        );
        return false;
      }
      return true;
    },
    // `account` is present only on the sign-in request itself, so the flag is
    // set once and then rides the JWT.
    jwt: ({ token, account }) => {
      if (account) token.localOwner = account.provider === LOCAL_OWNER_PROVIDER_ID;
      return token;
    },
    session: ({ session, token }) => {
      session.localOwner = token.localOwner === true;
      return session;
    },
    // Re-runs on every request gated by the middleware (see middleware.ts),
    // so a user removed from the roster mid-session — or one whose JWT
    // predates the roster being installed — is refused on their next request
    // instead of coasting until their JWT expires. Returns a Response rather
    // than `false` so API routes get JSON 401s instead of Auth.js's default
    // HTML redirect.
    authorized: async ({ auth, request }) => {
      const verdict = await judgeSession(auth, request.headers.get("host"));
      if (verdict.kind === "allow") return true;
      if (verdict.kind === "revoke") {
        // Fire-and-forget audit so a mid-session eviction leaves a
        // trail even if the user never re-attempts sign-in.
        auditAuth(
          "auth_logout",
          `Session revoked: ${verdict.email} (${describeDenial(verdict.decision.source)})`,
          verdict.email,
          {
            revoked: true,
            reason: "not_in_allowlist",
            source: verdict.decision.source,
            // Always false on this branch; read off the decision anyway so
            // it cannot silently desync from decideSessionAction.
            roster_unavailable: verdict.decision.rosterUnknown,
          },
        );
      }
      return refuse(request);
    },
  },
  events: {
    signIn: ({ user, account }) => {
      if (account?.provider === LOCAL_OWNER_PROVIDER_ID) {
        auditAuth("auth_login", "Login: the owner, on this computer (one-person mode)", null, {
          provider: LOCAL_OWNER_PROVIDER_ID,
        });
        return;
      }
      const email = user.email?.toLowerCase() ?? null;
      auditAuth("auth_login", `Login: ${email ?? "unknown"}`, email, { provider: "google" });
    },
    signOut: (message) => {
      // JWT strategy sends { token }, session strategy sends { session }.
      const token = "token" in message ? message.token : undefined;
      if (token?.localOwner === true) {
        auditAuth("auth_logout", "Logout: the owner, on this computer (one-person mode)", null, {});
        return;
      }
      const email = typeof token?.email === "string" ? token.email.toLowerCase() : null;
      auditAuth("auth_logout", `Logout: ${email ?? "unknown"}`, email, {});
    },
  },
});
