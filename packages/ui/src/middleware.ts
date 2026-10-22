import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server";
import { auth } from "@/auth";
import { contentSecurityPolicy, makeNonce } from "@/lib/csp";
import { HOST_CALLS, HOST_ORIGINS } from "@/lib/hostConfig";
import { backendSegments, corsHeaders, frameAncestors, hostPreflight, judgeHostRequest } from "@/lib/hostOrigins";
import { SESSION_FREE_PATH } from "@/lib/middlewarePaths";

// Every request but static assets comes through here, in this order:
//
// 1. A call to the backend proxy from a host origin (lib/hostOrigins.ts): its
//    preflight is answered here, before any session check (a preflight
//    carries no cookie), and a call off the host list is refused with 403.
//    Both with CORS for that exact origin, so the host's page can read why.
// 2. The session check, except for Auth.js's own routes and exactly `/signin`
//    (with optional trailing slash): the decision, and the response a refused
//    request gets (JSON 401 for /api/*, a redirect to /signin for pages),
//    live in the `authorized` callback in auth.ts. A host call refused there
//    gets its 401 with CORS too, so a host's page can offer sign-in.
// 3. Pages get the Content-Security-Policy (lib/csp.ts): a nonce per
//    request, which Next.js puts on its own scripts, and who may frame them.
//
// `auth` is called as-is, never wrapped as `auth((req) => …)`: when a wrapper
// is passed, next-auth runs it even after `authorized` returns false, so the
// per-request re-check never actually refused anyone — a user removed from
// the roster kept working until their 24h JWT expired.
const sessionGate = auth as unknown as (req: NextRequest, ev: NextFetchEvent) => Promise<Response>;

const FRAME_ANCESTORS = frameAncestors(HOST_ORIGINS.origins);
// Written out literally: Next.js folds it at build time, so a production
// build never allows eval.
const DEV_SERVER = process.env.NODE_ENV !== "production";

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

async function backend(req: NextRequest, ev: NextFetchEvent, segments: string[]): Promise<Response> {
  const origin = req.headers.get("origin");
  const secFetchSite = req.headers.get("sec-fetch-site");
  const preflight = hostPreflight(
    {
      origin,
      secFetchSite,
      method: req.method,
      segments,
      requestMethod: req.headers.get("access-control-request-method"),
      requestHeaders: req.headers.get("access-control-request-headers"),
    },
    HOST_ORIGINS,
    HOST_CALLS,
  );
  if (preflight) return new Response(null, preflight);

  const verdict = judgeHostRequest({ origin, secFetchSite, method: req.method, segments }, HOST_ORIGINS, HOST_CALLS);
  if (verdict.kind === "refused") {
    return json(403, { error: "not available from this page", code: verdict.reason }, corsHeaders(verdict.origin));
  }
  const response = await sessionGate(req, ev);
  // Passed on: the proxy route adds CORS to its own answer (adding it here
  // too would send the header twice, which browsers refuse).
  if (verdict.kind === "host" && response.headers.get("x-middleware-next") !== "1") {
    for (const [name, value] of Object.entries(corsHeaders(verdict.origin))) response.headers.set(name, value);
  }
  return response;
}

export async function middleware(req: NextRequest, ev: NextFetchEvent): Promise<Response> {
  const { pathname } = req.nextUrl;
  const segments = backendSegments(pathname);
  if (segments !== null) return backend(req, ev, segments);

  const sessionFree = SESSION_FREE_PATH.test(pathname);
  if (pathname.startsWith("/api/")) {
    if (!sessionFree) return sessionGate(req, ev);
    // Auth.js's own pages (sign-out, its error page) are HTML too: no other
    // site may frame them. Its inline scripts get no nonce, so only this.
    const response = NextResponse.next();
    // Not even a host origin: the embedded sign-in is a top-level popup.
    response.headers.set("Content-Security-Policy", "frame-ancestors 'self'");
    return response;
  }

  const csp = contentSecurityPolicy({ nonce: makeNonce(), frameAncestors: FRAME_ANCESTORS, devServer: DEV_SERVER });
  const requestHeaders = new Headers(req.headers);
  // Next.js reads the nonce from the request's policy and puts it on every
  // script it renders.
  requestHeaders.set("content-security-policy", csp);
  const passOn = () => NextResponse.next({ request: { headers: requestHeaders } });

  let response: Response;
  if (sessionFree) {
    response = passOn();
  } else {
    const gated = await sessionGate(req, ev);
    if (gated.headers.get("x-middleware-next") === "1") {
      // Allowed: pass the page on with the nonce, keeping any cookie the
      // session check refreshed.
      response = passOn();
      for (const cookie of gated.headers.getSetCookie()) response.headers.append("set-cookie", cookie);
    } else {
      response = gated;
    }
  }
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

// Everything but Next's static files and the favicon. An entry with no
// terminator would exclude every path that merely starts with it, so the
// favicon is an exact match: a future `/favicon.icoX` still runs here.
// Checked against Next's own matcher compiler in
// scripts/middleware-matcher.test.mjs.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon\\.ico$).*)"],
};
