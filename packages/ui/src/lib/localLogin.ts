// Local login: running Open Executive on your own computer without setting
// up Google sign-in. The sign-in page offers an "Open" button instead, and the
// session it creates carries no email — so the UI proxy sends no
// `x-caller-email`, and the backend treats the caller as the principal, exactly
// as it does for the CLI.
//
// There is no password, so the guard is WHERE a request can come from:
//   1. `make dev` is the only thing that sets OE_LOCAL_LOGIN, and the same
//      recipe line starts the UI with `-H 127.0.0.1`, so nothing off this
//      machine can connect. Docker (`make docker`) and deploy images never set
//      it, and a production build compiles local login out (see auth.ts).
//   2. Every sign-in and every request must carry a loopback `Host` header.
//      That stops a DNS-rebinding page — a site the owner visits that re-points
//      its own hostname at 127.0.0.1 — because a browser always sends the
//      page's real hostname as `Host` and scripts cannot override it. Scripts
//      CAN set `X-Forwarded-Host`, which is why the raw header is read and not
//      `request.url` (Auth.js rebuilds that from X-Forwarded-Host under
//      trustHost).
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/localLogin.test.mjs).

/** The Auth.js provider id, and the id of the one user it signs in. */
export const LOCAL_LOGIN_PROVIDER_ID = "local-login";

export interface LocalLoginEnv {
  /** False in a production build (`next build`); the mode never applies there. */
  devServer: boolean;
  /** OE_LOCAL_LOGIN — set only by `make dev`, with the loopback bind. */
  flag: string | undefined;
  /** AUTH_GOOGLE_ID — once Google sign-in is set up, it is the only way in. */
  googleClientId: string | undefined;
  /** OE_PUBLIC_DEPLOYMENT — an internet-facing instance always requires sign-in. */
  publicDeployment: string | undefined;
}

// Mirrors api/main.py's _FALSEY_ENV, so both apps read OE_PUBLIC_DEPLOYMENT
// alike; scripts/localLogin.test.mjs fails if the two drift apart.
export const FALSEY_ENV: ReadonlySet<string> = new Set(["", "0", "false", "no", "off"]);

export function localLoginEnabled(env: LocalLoginEnv): boolean {
  return (
    env.devServer &&
    env.flag?.trim() === "1" &&
    !env.googleClientId?.trim() &&
    FALSEY_ENV.has((env.publicDeployment ?? "").trim().toLowerCase())
  );
}

const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * True when a raw `Host` header names this machine: `localhost`,
 * `127.0.0.1` or `[::1]`, with or without a numeric port. Anything else —
 * a LAN address, `localhost.example.com`, a missing header — is false.
 */
export function isLoopbackHost(host: string | null | undefined): boolean {
  if (!host) return false;
  const match = /^(\[[0-9a-f:.]+\]|[^:[\]]+)(?::(\d{1,5}))?$/.exec(host.trim().toLowerCase());
  return match !== null && LOOPBACK_HOSTNAMES.has(match[1]);
}

/**
 * Whether a local-login session may be used for this request. It stays
 * valid only while the mode is on and the request still comes from this
 * machine — so setting up Google sign-in ends it on the next request.
 */
export function localLoginSessionAllowed(modeEnabled: boolean, host: string | null | undefined): boolean {
  return modeEnabled && isLoopbackHost(host);
}
