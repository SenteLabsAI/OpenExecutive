// What the middleware does with a request (lib/pageGate.ts, wired in by
// src/middleware.ts), with the session check stubbed: who is let through,
// what a refusal looks like, and that every page and refusal of one carries
// the Content-Security-Policy.
import assert from "node:assert/strict";
import test from "node:test";
import { PASSED_ON_HEADER, gatePage } from "../src/lib/pageGate.ts";

const POLICY = "script-src 'self' 'nonce-abc'; frame-ancestors 'self'";
const AUTH_PAGES = "frame-ancestors 'self'";

// NextResponse.next(): a response marked as passed on, and the request
// headers it hands to the page, recorded for the test.
function harness(gateResult) {
  const seen = { gateCalls: 0, requestHeaders: undefined };
  const deps = {
    sessionGate: async () => {
      seen.gateCalls += 1;
      if (gateResult instanceof Error) throw gateResult;
      return typeof gateResult === "function" ? gateResult() : gateResult;
    },
    passOn: (requestHeaders) => {
      seen.requestHeaders = requestHeaders;
      return new Response(null, { headers: { [PASSED_ON_HEADER]: "1" } });
    },
    csp: () => POLICY,
    authPagesPolicy: AUTH_PAGES,
  };
  return { deps, seen };
}

const request = (pathname, sessionFree = false) => ({
  pathname,
  headers: new Headers({ cookie: "session=old", accept: "text/html" }),
  sessionFree,
});

test("allowed: the page goes on with the policy on both sides and the refreshed cookie", async () => {
  const allowed = new Response(null, { headers: { [PASSED_ON_HEADER]: "1" } });
  allowed.headers.append("set-cookie", "session=new; Path=/; HttpOnly");
  const { deps, seen } = harness(allowed);
  const response = await gatePage(request("/today"), deps);
  assert.equal(seen.gateCalls, 1);
  assert.equal(response.headers.get("content-security-policy"), POLICY);
  assert.equal(seen.requestHeaders.get("content-security-policy"), POLICY);
  // The page still gets the browser's own headers.
  assert.equal(seen.requestHeaders.get("cookie"), "session=old");
  assert.deepEqual(response.headers.getSetCookie(), ["session=new; Path=/; HttpOnly"]);
});

test("refused page: the sign-in redirect itself carries the policy", async () => {
  // Response.redirect() answers with headers that can't be changed.
  const { deps, seen } = harness(() => Response.redirect("https://oe.example.com/signin?callbackUrl=%2Ftoday"));
  const response = await gatePage(request("/today"), deps);
  assert.equal(response.status, 302);
  assert.equal(response.headers.get("location"), "https://oe.example.com/signin?callbackUrl=%2Ftoday");
  assert.equal(response.headers.get("content-security-policy"), POLICY);
  // Not passed on: the page never rendered.
  assert.equal(seen.requestHeaders, undefined);
});

test("refused API call: the check's own 401, untouched", async () => {
  const { deps } = harness(() => Response.json({ error: "unauthorized" }, { status: 401 }));
  const response = await gatePage(request("/api/backend/today"), deps);
  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { error: "unauthorized" });
});

test("session-free: the sign-in page gets the policy without a session check", async () => {
  const { deps, seen } = harness(new Response(null));
  const response = await gatePage(request("/signin", true), deps);
  assert.equal(seen.gateCalls, 0);
  assert.equal(response.headers.get("content-security-policy"), POLICY);
  assert.equal(seen.requestHeaders.get("content-security-policy"), POLICY);
});

test("session-free API (Auth.js's own pages): framing only, no session check", async () => {
  const { deps, seen } = harness(new Response(null));
  const response = await gatePage(request("/api/auth/signout", true), deps);
  assert.equal(seen.gateCalls, 0);
  assert.equal(response.headers.get("content-security-policy"), AUTH_PAGES);
});

for (const [name, result] of [
  ["nothing", undefined],
  ["null", null],
  ["a plain object", { ok: true }],
  ["an object with headers but no status", { headers: new Headers({ [PASSED_ON_HEADER]: "1" }) }],
]) {
  test(`a check that answers ${name} lets no page through`, async () => {
    const { deps, seen } = harness(result);
    const page = await gatePage(request("/today"), deps);
    assert.equal(page.status, 500);
    assert.equal(page.headers.get("content-security-policy"), POLICY);
    assert.equal(seen.requestHeaders, undefined);
    const api = await gatePage(request("/api/backend/today"), deps);
    assert.equal(api.status, 500);
    assert.equal(seen.requestHeaders, undefined);
  });
}

test("a refusal without the passed-on mark is never treated as allowed", async () => {
  const { deps, seen } = harness(() => new Response("no", { status: 403, headers: { [PASSED_ON_HEADER]: "0" } }));
  const response = await gatePage(request("/today"), deps);
  assert.equal(response.status, 403);
  assert.equal(seen.requestHeaders, undefined);
  assert.equal(response.headers.get("content-security-policy"), POLICY);
});

test("a check that throws lets nothing through", async () => {
  const { deps } = harness(new Error("session store down"));
  await assert.rejects(gatePage(request("/today"), deps), /session store down/);
});
