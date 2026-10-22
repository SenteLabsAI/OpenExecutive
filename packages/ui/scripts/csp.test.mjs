// The pages' Content-Security-Policy (lib/csp.ts, set by src/middleware.ts).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { contentSecurityPolicy, makeNonce } from "../src/lib/csp.ts";
import { frameAncestors, parseHostOrigins } from "../src/lib/hostOrigins.ts";

const directives = (policy) =>
  Object.fromEntries(policy.split(";").map((d) => d.trim().split(/\s+/)).map(([name, ...values]) => [name, values]));

test("scripts need this response's nonce; nothing inline or eval'd", () => {
  const nonce = makeNonce();
  const policy = directives(contentSecurityPolicy({ nonce, frameAncestors: frameAncestors([]), devServer: false }));
  assert.deepEqual(policy["script-src"], ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"]);
  assert.deepEqual(policy["object-src"], ["'none'"]);
  assert.deepEqual(policy["base-uri"], ["'self'"]);
  const all = Object.values(policy).flat();
  assert.ok(!all.includes("'unsafe-inline'") && !all.includes("'unsafe-eval'"));
});

test("only the dev server allows eval, for React's dev build", () => {
  const policy = directives(contentSecurityPolicy({ nonce: makeNonce(), frameAncestors: frameAncestors([]), devServer: true }));
  assert.ok(policy["script-src"].includes("'unsafe-eval'"));
  assert.ok(!policy["script-src"].includes("'unsafe-inline'"));
});

test("framing: this app alone unless host origins are set", () => {
  const unset = directives(contentSecurityPolicy({ nonce: makeNonce(), frameAncestors: frameAncestors([]), devServer: false }));
  assert.deepEqual(unset["frame-ancestors"], ["'self'"]);
  const hosts = parseHostOrigins("https://app.example.com").origins;
  const set = directives(contentSecurityPolicy({ nonce: makeNonce(), frameAncestors: frameAncestors(hosts), devServer: false }));
  assert.deepEqual(set["frame-ancestors"], ["'self'", "https://app.example.com"]);
});

test("every nonce is fresh and well formed", () => {
  const a = makeNonce();
  assert.notEqual(a, makeNonce());
  assert.equal(Buffer.from(a, "base64").length, 16);
  assert.throws(() => contentSecurityPolicy({ nonce: "x'; script-src *", frameAncestors: "", devServer: false }));
});

test("every page renders on request, so each gets its own nonce", () => {
  // A page built ahead of time would carry no nonce, and the policy would
  // stop its scripts: the root layout waits for the request.
  const layout = readFileSync(new URL("../src/app/layout.tsx", import.meta.url), "utf8");
  assert.match(layout, /await connection\(\)/);
  const middleware = readFileSync(new URL("../src/middleware.ts", import.meta.url), "utf8");
  assert.match(middleware, /requestHeaders\.set\("content-security-policy", csp\)/);
  assert.match(middleware, /response\.headers\.set\("Content-Security-Policy", csp\)/);
});
