// Which paths the auth middleware skips.
//
// `config.matcher` in src/middleware.ts is a negative lookahead: every entry
// in it is a path the session check never sees. An entry with no terminator
// excludes every path that merely *starts with* it — `api/auth` used to skip
// `/api/authorizations`, and `favicon.ico` skipped `/favicon.icoX` and (the dot
// being unescaped) `/faviconXico` — so a future route with such a prefix would
// be served with no session check at all.
//
// The matcher is not a plain RegExp: Next rewrites it through its vendored
// path-to-regexp, which is where escaping goes wrong (`\\.` in the TS source is
// right; `\\\\.` would match a literal backslash). So this compiles the string
// with Next's own functions — the ones that write middleware-manifest.json and
// match requests against it — instead of re-implementing them. middleware.ts
// itself can't be imported here (`@/` aliases, next-auth), and Next only reads
// a literal matcher anyway, so the literal is read from the source.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { getMiddlewareMatchers } = require("next/dist/build/analysis/get-page-static-info.js");
const { getMiddlewareRouteMatcher } = require("next/dist/shared/lib/router/utils/middleware-route-matcher.js");

const source = readFileSync(new URL("../src/middleware.ts", import.meta.url), "utf8");
const literal = source.match(/matcher:\s*\[\s*("(?:[^"\\]|\\.)*")\s*,?\s*\]/);
assert.ok(literal, "could not find a single string literal in middleware.ts config.matcher");

// next.config.ts sets neither basePath nor i18n, the only config this reads.
const matcher = getMiddlewareRouteMatcher(getMiddlewareMatchers([JSON.parse(literal[1])], {}));
const runsMiddleware = (pathname) => matcher(pathname, { headers: {}, cookies: {} }, {});

test("Auth.js routes, static assets and the sign-in page skip the middleware", () => {
  for (const path of [
    "/api/auth",
    "/api/auth/",
    "/api/auth/session",
    "/api/auth/providers",
    "/api/auth/callback/google",
    "/favicon.ico",
    "/_next/static/chunks/main.js",
    "/signin",
  ]) {
    assert.equal(runsMiddleware(path), false, path);
  }
});

test("a path that only starts with an excluded entry is still gated", () => {
  for (const path of [
    "/api/authX",
    "/api/authorizations",
    "/api/auth-anything",
    "/favicon.icoX",
    "/faviconXico",
    "/favicon.ico/x",
    "/signin-help",
  ]) {
    assert.equal(runsMiddleware(path), true, path);
  }
});

test("ordinary pages and API routes are gated", () => {
  for (const path of ["/", "/settings", "/api/backend/health"]) {
    assert.equal(runsMiddleware(path), true, path);
  }
});
