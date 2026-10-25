import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server";
import { auth } from "@/auth";
import { contentSecurityPolicy, FRAME_ANCESTORS, makeNonce } from "@/lib/csp";
import { SESSION_FREE_PATH } from "@/lib/middlewarePaths";
import { gatePage } from "@/lib/pageGate";

// Every request but static assets comes through here. What it does with one
// (the session check, then the Content-Security-Policy on every page and
// every refusal of one) is lib/pageGate.ts; this wires in Next.js and
// Auth.js.
//
// `auth` is called as-is, never wrapped as `auth((req) => …)`: when a wrapper
// is passed, next-auth runs it even after `authorized` returns false, so the
// per-request re-check never actually refused anyone — a user removed from
// the roster kept working until their 24h JWT expired.
const sessionGate = auth as unknown as (req: NextRequest, ev: NextFetchEvent) => Promise<unknown>;

// Written out literally: Next.js folds it at build time, so a production
// build never allows eval.
const DEV_SERVER = process.env.NODE_ENV !== "production";

export function middleware(req: NextRequest, ev: NextFetchEvent): Promise<Response> {
  const { pathname } = req.nextUrl;
  return gatePage(
    { pathname, headers: req.headers, sessionFree: SESSION_FREE_PATH.test(pathname) },
    {
      sessionGate: () => sessionGate(req, ev),
      passOn: (requestHeaders) =>
        requestHeaders ? NextResponse.next({ request: { headers: requestHeaders } }) : NextResponse.next(),
      csp: () => contentSecurityPolicy({ nonce: makeNonce(), devServer: DEV_SERVER }),
      authPagesPolicy: FRAME_ANCESTORS,
    },
  );
}

// Everything but Next's static files and the favicon. An entry with no
// terminator would exclude every path that merely starts with it, so the
// favicon is an exact match: a future `/favicon.icoX` still runs here.
// Checked against Next's own matcher compiler in
// scripts/middleware-matcher.test.mjs.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon\\.ico$).*)"],
};
