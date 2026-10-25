// The pages' Content-Security-Policy (lib/csp.ts, set by src/middleware.ts).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { contentSecurityPolicy, makeNonce } from "../src/lib/csp.ts";

const directives = (policy) =>
  Object.fromEntries(policy.split(";").map((d) => d.trim().split(/\s+/)).map(([name, ...values]) => [name, values]));

test("scripts need this response's nonce; nothing inline or eval'd", () => {
  const nonce = makeNonce();
  const policy = directives(contentSecurityPolicy({ nonce, devServer: false }));
  assert.deepEqual(policy["script-src"], ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"]);
  assert.deepEqual(policy["object-src"], ["'none'"]);
  assert.deepEqual(policy["base-uri"], ["'self'"]);
  const all = Object.values(policy).flat();
  assert.ok(!all.includes("'unsafe-inline'") && !all.includes("'unsafe-eval'"));
});

test("only the dev server allows eval, for React's dev build", () => {
  const policy = directives(contentSecurityPolicy({ nonce: makeNonce(), devServer: true }));
  assert.ok(policy["script-src"].includes("'unsafe-eval'"));
  assert.ok(!policy["script-src"].includes("'unsafe-inline'"));
});

test("no other site may frame the pages", () => {
  const policy = directives(contentSecurityPolicy({ nonce: makeNonce(), devServer: false }));
  assert.deepEqual(policy["frame-ancestors"], ["'self'"]);
});

test("every nonce is fresh and well formed", () => {
  const a = makeNonce();
  assert.notEqual(a, makeNonce());
  assert.equal(Buffer.from(a, "base64").length, 16);
  assert.throws(() => contentSecurityPolicy({ nonce: "x'; script-src *", devServer: false }));
});

test("every page renders on request, so each gets its own nonce", () => {
  // A page built ahead of time would carry no nonce, and the policy would
  // stop its scripts: the root layout waits for the request.
  const layout = readFileSync(new URL("../src/app/layout.tsx", import.meta.url), "utf8");
  assert.match(layout, /await connection\(\)/);
  // What the middleware does with the policy is tested with a stub session
  // check in pageGate.test.mjs; here, only that it is wired to lib/pageGate.ts.
  const middleware = readFileSync(new URL("../src/middleware.ts", import.meta.url), "utf8");
  assert.match(middleware, /return gatePage\(/);
  assert.match(middleware, /csp: \(\) => contentSecurityPolicy\(\{ nonce: makeNonce\(\), devServer: DEV_SERVER \}\)/);
});
