import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server";
import { auth } from "@/auth";
import { contentSecurityPolicy, FRAME_ANCESTORS, makeNonce } from "@/lib/csp";
import { SESSION_FREE_PATH } from "@/lib/middlewarePaths";

// Every request but static assets comes through here:
//
// 1. The session check, except for Auth.js's own routes and exactly `/signin`
//    (with optional trailing slash): the decision, and the response a refused
//    request gets (JSON 401 for /api/*, a redirect to /signin for pages),
//    live in the `authorized` callback in auth.ts.
// 2. Pages get the Content-Security-Policy (lib/csp.ts): a nonce per
//    request, which Next.js puts on its own scripts, and no framing by
//    other sites. The sign-in page gets it too, which is why it now runs
//    here, though it needs no session.
//
// `auth` is called as-is, never wrapped as `auth((req) => …)`: when a wrapper
// is passed, next-auth runs it even after `authorized` returns false, so the
// per-request re-check never actually refused anyone — a user removed from
// the roster kept working until their 24h JWT expired.
const sessionGate = auth as unknown as (req: NextRequest, ev: NextFetchEvent) => Promise<Response>;

// Written out literally: Next.js folds it at build time, so a production
// build never allows eval.
const DEV_SERVER = process.env.NODE_ENV !== "production";

export async function middleware(req: NextRequest, ev: NextFetchEvent): Promise<Response> {
  const { pathname } = req.nextUrl;
  const sessionFree = SESSION_FREE_PATH.test(pathname);
  if (pathname.startsWith("/api/")) {
    if (!sessionFree) return sessionGate(req, ev);
    // Auth.js's own pages (sign-out, its error page) are HTML too: no other
    // site may frame them. Its inline scripts get no nonce, so only this.
    const response = NextResponse.next();
    response.headers.set("Content-Security-Policy", FRAME_ANCESTORS);
    return response;
  }

  const csp = contentSecurityPolicy({ nonce: makeNonce(), devServer: DEV_SERVER });
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
