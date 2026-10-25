// Host origins (lib/hostOrigins.ts, docs/auth.md "Host origins"): who may
// frame the pages and which proxy calls a host origin's page may make.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  backendSegments,
  corsHeaders,
  frameAncestors,
  hostCallAllowed,
  hostPreflight,
  isBrowserOriginHeader,
  judgeHostRequest,
  parseHostOrigins,
} from "../src/lib/hostOrigins.ts";

const HOST_LIST = JSON.parse(readFileSync(new URL("../src/lib/host-list.json", import.meta.url), "utf8"));
const CALLS = HOST_LIST.calls;
const HOST = "https://app.example.com";
const HOSTS = parseHostOrigins(HOST);

const seg = (path) => backendSegments(`/api/backend${path}`);
const call = (method, path, extra = {}) =>
  judgeHostRequest({ origin: HOST, secFetchSite: "same-site", method, segments: seg(path), ...extra }, HOSTS, CALLS);

test("OE_HOST_ORIGINS lists exact origins, and a bad entry refuses them all", () => {
  assert.deepEqual(parseHostOrigins(undefined), { origins: [], error: null });
  assert.deepEqual(parseHostOrigins("  "), { origins: [], error: null });
  assert.deepEqual(parseHostOrigins("https://APP.example.com/, https://b.example.com:8443").origins, [
    "https://app.example.com",
    "https://b.example.com:8443",
  ]);
  assert.deepEqual(parseHostOrigins("http://app.example.localhost:8088").origins, ["http://app.example.localhost:8088"]);
  for (const bad of [
    "http://app.example.com",
    "https://app.example.com/desk",
    "https://*.example.com",
    "app.example.com",
    "https://app.example.com?x",
    "javascript:alert(1)",
  ]) {
    const parsed = parseHostOrigins(`${HOST} ${bad}`);
    assert.deepEqual(parsed.origins, [], bad);
    assert.ok(parsed.error, bad);
  }
});

test("frame-ancestors names this app and the host origins only", () => {
  assert.equal(frameAncestors([]), "frame-ancestors 'self'");
  assert.equal(frameAncestors(HOSTS.origins), `frame-ancestors 'self' ${HOST}`);
});

test("only a call on the host list passes, from the exact origin, same-site", () => {
  assert.deepEqual(call("GET", "/today"), { kind: "host", origin: HOST });
  assert.deepEqual(call("POST", "/chat"), { kind: "host", origin: HOST });
  assert.deepEqual(call("POST", "/alerts/12/ack"), { kind: "host", origin: HOST });
  assert.deepEqual(call("GET", "/sessions/abc-123/messages"), { kind: "host", origin: HOST });
  for (const [method, path] of [
    ["POST", "/decisions/12/approve"],
    ["POST", "/decisions/12/reject"],
    ["POST", "/decisions/12/cancel"],
    ["POST", "/alerts/12/approve"],
    ["GET", "/people"],
    ["PUT", "/settings/notifications"],
    ["POST", "/delegation/gmail/send"],
    ["DELETE", "/sessions/abc"],
    ["GET", "/today/../people"],
    ["POST", "/alerts/../ack"],
    ["GET", "/watchlist/a%3Fb"],
    ["GET", "/watchlist/a%2Fb"],
    ["GET", "/today/"],
  ]) {
    assert.deepEqual(call(method, path), { kind: "refused", origin: HOST, reason: "not_on_host_list" }, `${method} ${path}`);
  }
  assert.equal(call("GET", "/today", { secFetchSite: "cross-site" }).reason, "not_same_site");
  assert.equal(call("GET", "/today", { secFetchSite: null }).reason, "not_same_site");
  assert.equal(call("GET", "/today", { secFetchSite: "same-origin" }).reason, "not_same_site");
});

test("anything not from a host origin is left to the app's own rules", () => {
  for (const origin of [null, "", "https://exec.example.com", "https://app.example.com.evil.test", "https://app.example.com:444"]) {
    assert.deepEqual(call("POST", "/decisions/1/approve", { origin }), { kind: "own" }, String(origin));
  }
  // With no host origins set, nothing is a host call.
  assert.deepEqual(
    judgeHostRequest({ origin: HOST, secFetchSite: "same-site", method: "GET", segments: ["today"] }, parseHostOrigins(""), CALLS),
    { kind: "own" },
  );
});

test("a pattern's {name} is one plain segment", () => {
  const calls = [["GET", "/watchlist/{slug}"]];
  assert.equal(hostCallAllowed(calls, "get", ["watchlist", "price-of-steel"]), true);
  for (const bad of ["", ".", "..", "a/b", "a?b", "a#b", "a\\b", "a b", "a%2e"]) {
    assert.equal(hostCallAllowed(calls, "GET", ["watchlist", bad]), false, JSON.stringify(bad));
  }
  assert.equal(hostCallAllowed(calls, "GET", ["watchlist"]), false);
  assert.equal(hostCallAllowed(calls, "POST", ["watchlist", "x"]), false);
});

test("CORS is for that exact origin, with credentials", () => {
  assert.deepEqual(corsHeaders(HOST), {
    "Access-Control-Allow-Origin": HOST,
    "Access-Control-Allow-Credentials": "true",
    Vary: "Origin",
  });
});

test("a host's preflight is answered for a listed call only", () => {
  const preflight = (extra) =>
    hostPreflight(
      {
        origin: HOST,
        secFetchSite: "same-site",
        method: "OPTIONS",
        segments: seg("/chat"),
        requestMethod: "POST",
        requestHeaders: "content-type, x-anything",
        ...extra,
      },
      HOSTS,
      CALLS,
    );
  assert.deepEqual(preflight({}), {
    status: 204,
    headers: {
      ...corsHeaders(HOST),
      "Access-Control-Allow-Methods": "POST",
      "Access-Control-Allow-Headers": "content-type",
      "Access-Control-Max-Age": "600",
    },
  });
  assert.equal(preflight({ segments: seg("/decisions/1/approve") }).status, 403);
  assert.equal(preflight({ requestMethod: "DELETE" }).status, 403);
  assert.equal(preflight({ requestMethod: null }).status, 403);
  assert.equal(preflight({ secFetchSite: "cross-site" }).status, 403);
  // Not from a host origin, or not a preflight: not this rule's to answer.
  assert.equal(preflight({ origin: "https://other.example.com" }), null);
  assert.equal(preflight({ method: "POST" }), null);
});

test("the browser's own origin headers are what a host call drops", () => {
  for (const name of ["Origin", "referer", "sec-fetch-site", "Sec-Fetch-Mode", "sec-fetch-dest"]) {
    assert.equal(isBrowserOriginHeader(name), true, name);
  }
  for (const name of ["content-type", "accept", "x-caller-surface", "cookie"]) {
    assert.equal(isBrowserOriginHeader(name), false, name);
  }
});

test("the proxy marks a host call's surface and signs it", () => {
  const route = readFileSync(new URL("../src/app/api/backend/[...path]/route.ts", import.meta.url), "utf8");
  assert.match(route, /headers\.set\("x-caller-surface", "host"\)/);
  assert.match(route, /fromHost \? \{ surface: "host" as const \} : \{\}/);
  // A client's own x-caller-surface is stripped with the rest of the family.
  assert.match(route, /lower\.startsWith\("x-caller-"\)/);
  // Nor does a session cookie the middleware refreshed reach the API.
  assert.match(route, /lower === "set-cookie"/);
  // What the API answers with no policy of its own runs nothing here.
  assert.match(route, /"default-src 'none'; frame-ancestors 'none'; sandbox"/);
});
