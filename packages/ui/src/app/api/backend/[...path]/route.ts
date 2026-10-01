import { NextRequest } from "next/server";
import { LOCAL_LOGIN, auth } from "@/auth";
import {
  CALLER_ASSERTION_HEADER,
  mintCallerAssertion,
  parseCallerSigningKey,
  type CallerSigner,
} from "@/lib/callerAssertion";
import { isCrossSiteWrite } from "@/lib/crossSite";
import { localLoginSessionAllowed } from "@/lib/localLogin";

// Streaming-aware proxy to the FastAPI backend. Replaces the `rewrites()` rule
// in next.config.ts, which buffers SSE responses in dev so the chat stream
// arrives in one chunk at the end of the turn — making the Agent Activity
// panel look frozen.
//
// `runtime = "nodejs"` is required because the Edge runtime can't easily
// stream a fetched body without buffering either.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_BASE = process.env.BACKEND_BASE_URL ?? "http://localhost:8000";
const BACKEND_SHARED_SECRET = process.env.BACKEND_SHARED_SECRET ?? "";

// Signed callers (lib/callerAssertion.ts, docs/auth.md): with
// CALLER_ASSERTION_PRIVATE_KEY set, every request also carries a signed
// statement of who is calling, which the API checks. A key that can't be read
// refuses every request rather than let one go out unsigned.
let CALLER_SIGNER: CallerSigner | null = null;
let CALLER_SIGNER_BROKEN = false;
try {
  CALLER_SIGNER = parseCallerSigningKey(process.env.CALLER_ASSERTION_PRIVATE_KEY);
} catch (err) {
  CALLER_SIGNER_BROKEN = true;
  console.error(`[proxy] ${err instanceof Error ? err.message : "CALLER_ASSERTION_PRIVATE_KEY can't be used"}`);
}

async function proxy(req: NextRequest, params: { path: string[] }): Promise<Response> {
  // Another page on this site (any localhost port counts) must not be able to
  // make the browser post here with the user's cookie — see lib/crossSite.ts.
  if (isCrossSiteWrite(req.method, req.headers.get("sec-fetch-site"))) {
    return new Response(JSON.stringify({ error: "cross-site request refused" }), {
      status: 403,
      headers: { "content-type": "application/json" },
    });
  }

  // Belt-and-suspenders: middleware should have already rejected unauthenticated
  // traffic, but check here too so a stray client can't reach the backend.
  const session = await auth();
  // A local-login session has no email on purpose: the backend reads a
  // request with no `x-caller-email` as the principal (the CLI's fallback).
  // So it is re-checked here, where it reaches the API, and every OTHER
  // session must carry an email — without one it would be read the same way.
  const callerEmail = session?.user?.email?.toLowerCase();
  const allowed = session?.localLogin
    ? localLoginSessionAllowed(LOCAL_LOGIN, req.headers.get("host"))
    : Boolean(callerEmail);
  if (!allowed) {
    return new Response(JSON.stringify({ error: "unauthorized" }), {
      status: 401,
      headers: { "content-type": "application/json" },
    });
  }
  if (CALLER_SIGNER_BROKEN) {
    return new Response(JSON.stringify({ error: "caller signing is misconfigured" }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  }

  const path = params.path.join("/");
  const url = new URL(`${BACKEND_BASE}/${path}`);
  // Preserve query string.
  req.nextUrl.searchParams.forEach((v, k) => url.searchParams.append(k, v));

  // Copy headers, drop hop-by-hop and Next.js internals. Also drop
  // the entire `x-caller-*` family — we re-stamp the caller identity
  // below from the verified NextAuth session, so a client sending any
  // `x-caller-*` header can never spoof identity. The prefix-strip
  // (rather than naming each header) is forward-proof: future caller
  // headers (x-caller-id, x-caller-roles, …) inherit the same
  // protection automatically.
  const headers = new Headers();
  req.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (
      lower === "host" ||
      lower === "connection" ||
      lower === "cookie" ||
      lower === "authorization" ||
      lower === "x-api-key" ||
      lower.startsWith("x-caller-") ||
      lower.startsWith("x-forwarded-")
    ) {
      return;
    }
    headers.set(key, value);
  });

  // Stamp the proxy's own identity on every upstream request. The API enforces
  // this header; direct hits to the public API URL without it get 401.
  if (BACKEND_SHARED_SECRET) {
    headers.set("x-api-key", BACKEND_SHARED_SECRET);
  }

  // Stamp the signed-in user's email so the backend can resolve them to
  // a Person row (used for Honcho per-person memory, future per-user
  // filtering on /audit, /today, etc.). Source: the verified NextAuth
  // session — clients have no way to set this themselves (stripped
  // above).
  if (callerEmail && !session?.localLogin) {
    headers.set("x-caller-email", callerEmail);
  }

  // Sign it: a local-login session is the operator (the owner at this
  // computer, naming no one), anyone else the email above. Bound to this
  // method and to the path and query exactly as fetch sends them.
  if (CALLER_SIGNER) {
    headers.set(
      CALLER_ASSERTION_HEADER,
      mintCallerAssertion(CALLER_SIGNER, {
        kind: session?.localLogin ? "operator" : "user",
        email: session?.localLogin ? "" : (callerEmail ?? ""),
        method: req.method,
        target: `${url.pathname}${url.search}`,
      }),
    );
  }

  const init: RequestInit = {
    method: req.method,
    headers,
    // Forward the body for non-GET/HEAD. `duplex: "half"` is required by
    // Node's fetch when streaming a request body.
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
    // Propagate a client disconnect upstream. Without this the backend never
    // sees `http.disconnect`, so its `request.is_disconnected()` check — and
    // the "persist the partial turn on disconnect" path behind it — never fire,
    // and a closed tab leaves the turn running to completion against Anthropic.
    signal: req.signal,
    // @ts-expect-error -- `duplex` is valid in Node fetch but not in the TS lib types yet.
    duplex: "half",
  };

  const upstream = await fetch(url, init);

  // Pass response through as a stream. Do not buffer.
  const respHeaders = new Headers(upstream.headers);
  // Hint to any downstream proxies (and Next's dev server) not to buffer SSE.
  // An upstream `no-store` (e.g. artifact downloads — confidential files)
  // is kept, so the browser never writes the body to its disk cache.
  const upstreamCache = upstream.headers.get("cache-control") ?? "";
  respHeaders.set(
    "Cache-Control",
    /no-store/i.test(upstreamCache) ? "no-store, no-transform" : "no-cache, no-transform"
  );
  respHeaders.set("X-Accel-Buffering", "no");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, await ctx.params);
}
export async function POST(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, await ctx.params);
}
export async function PATCH(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, await ctx.params);
}
export async function PUT(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, await ctx.params);
}
export async function DELETE(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, await ctx.params);
}
