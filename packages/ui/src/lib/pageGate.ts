// What src/middleware.ts does with a request, apart from Next.js and Auth.js
// themselves, so it can be tested with a stub session check
// (scripts/pageGate.test.mjs):
//
// 1. The session check, except for Auth.js's own routes and exactly `/signin`
//    (lib/middlewarePaths.ts). It answers with the request passed on, or with
//    the refusal auth.ts builds (JSON 401 for /api/*, a redirect to /signin
//    for pages). Anything else from it refuses: a check that breaks never
//    lets a page through.
// 2. Pages, the sign-in page and every refusal of one get the
//    Content-Security-Policy. A page passed on carries it on the request
//    too, where Next.js reads the nonce for its own scripts, and keeps any
//    session cookie the check refreshed.
//
// No imports, so `npm test` can exercise it.

// What NextResponse.next() sets on a response that lets the request through.
// The check says "allowed" only this way.
export const PASSED_ON_HEADER = "x-middleware-next";

export type PageGateDeps = {
  // Auth.js's middleware: the request passed on, or its refusal.
  sessionGate: () => Promise<unknown>;
  // NextResponse.next(), with the request's headers when given.
  passOn: (requestHeaders?: Headers) => Response;
  // A fresh policy for this response (lib/csp.ts).
  csp: () => string;
  // Auth.js's own HTML pages: framing only, since its inline scripts get no nonce.
  authPagesPolicy: string;
};

function refusal(status: number, json: boolean): Response {
  return json
    ? new Response(JSON.stringify({ error: "the session check failed" }), {
        status,
        headers: { "content-type": "application/json" },
      })
    : new Response("The session check failed.", { status, headers: { "content-type": "text/plain" } });
}

// By shape rather than instanceof: Next.js runs the middleware in a runtime
// of its own, where a Response may come from another realm.
function isResponse(value: unknown): value is Response {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as Response).status === "number" &&
    typeof (value as Response).headers?.get === "function"
  );
}

// Response.redirect() and others answer with headers that can't be changed;
// a copy can.
function withHeader(response: Response, name: string, value: string): Response {
  try {
    response.headers.set(name, value);
    return response;
  } catch {
    const copy = new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: new Headers(response.headers),
    });
    copy.headers.set(name, value);
    return copy;
  }
}

export async function gatePage(
  req: { pathname: string; headers: Headers; sessionFree: boolean },
  deps: PageGateDeps,
): Promise<Response> {
  if (req.pathname.startsWith("/api/")) {
    if (!req.sessionFree) {
      const gated = await deps.sessionGate();
      return isResponse(gated) ? gated : refusal(500, true);
    }
    return withHeader(deps.passOn(), "Content-Security-Policy", deps.authPagesPolicy);
  }

  const csp = deps.csp();
  const requestHeaders = new Headers(req.headers);
  requestHeaders.set("content-security-policy", csp);

  let response: Response;
  if (req.sessionFree) {
    response = deps.passOn(requestHeaders);
  } else {
    const gated = await deps.sessionGate();
    if (!isResponse(gated)) {
      response = refusal(500, false);
    } else if (gated.headers.get(PASSED_ON_HEADER) === "1") {
      // Allowed: pass the page on with the nonce, keeping any cookie the
      // session check refreshed.
      response = deps.passOn(requestHeaders);
      for (const cookie of gated.headers.getSetCookie()) response.headers.append("set-cookie", cookie);
    } else {
      response = gated;
    }
  }
  return withHeader(response, "Content-Security-Policy", csp);
}
