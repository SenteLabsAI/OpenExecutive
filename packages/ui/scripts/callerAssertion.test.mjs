import assert from "node:assert/strict";
import { createPublicKey, verify } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  ASSERTION_AUDIENCE,
  ASSERTION_LIFETIME_S,
  ASSERTION_VERSION,
  CALLER_ASSERTION_HEADER,
  mintCallerAssertion,
  parseCallerSigningKey,
} from "../src/lib/callerAssertion.ts";

// The same vectors packages/core/tests/unit/test_caller_assertion.py checks
// against scripts/mint-caller-assertion.py and the API's verifier: if this
// signer and those ever disagree by a byte, both suites fail.
const VECTORS = JSON.parse(
  readFileSync(new URL("../../core/tests/unit/caller_assertion_vectors.json", import.meta.url), "utf8"),
);
const CALLER_PY = readFileSync(new URL("../../core/openexecutive/api/caller.py", import.meta.url), "utf8");

function pyConstant(name) {
  const match = CALLER_PY.match(new RegExp(`^${name} = (.+)$`, "m"));
  assert.ok(match, `${name} not found in api/caller.py`);
  return JSON.parse(match[1]);
}

test("the constants match the API's", () => {
  assert.equal(ASSERTION_AUDIENCE, pyConstant("AUDIENCE"));
  assert.equal(ASSERTION_VERSION, pyConstant("ASSERTION_VERSION"));
  assert.equal(CALLER_ASSERTION_HEADER, pyConstant("CALLER_ASSERTION_HEADER"));
  // Room for a slow clock on either side inside what the API accepts.
  assert.ok(ASSERTION_LIFETIME_S < pyConstant("MAX_LIFETIME_S"));
});

for (const vector of VECTORS.cases) {
  test(`signs the shared vector: ${vector.kind} ${vector.method} ${vector.target}`, () => {
    const signer = parseCallerSigningKey(VECTORS.test_signing_key);
    const token = mintCallerAssertion(
      signer,
      { kind: vector.kind, email: vector.email, method: vector.method, target: vector.target },
      vector.now_ms,
      vector.jti,
    );
    assert.equal(token, vector.token);
  });
}

test("the signature checks out against the public key", () => {
  const [kid, encoded] = VECTORS.public_keys.split(":");
  const publicKey = createPublicKey({
    key: { kty: "OKP", crv: "Ed25519", x: encoded },
    format: "jwk",
  });
  const signer = parseCallerSigningKey(VECTORS.test_signing_key);
  assert.equal(signer.kid, kid);
  const token = mintCallerAssertion(signer, { kind: "user", email: "a@b.example", method: "get", target: "/today" });
  const [version, body, signature] = token.split(".");
  assert.equal(
    verify(null, Buffer.from(`${version}.${body}`), publicKey, Buffer.from(signature, "base64url")),
    true,
  );
  const claims = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
  assert.deepEqual(Object.keys(claims), ["aud", "exp", "iat", "jti", "kid", "kind", "m", "p", "sub"]);
  assert.equal(claims.m, "GET");
  assert.equal(claims.exp - claims.iat, ASSERTION_LIFETIME_S);
  assert.match(claims.jti, /^[A-Za-z0-9_-]{16,128}$/);
});

test("every assertion gets its own id", () => {
  const signer = parseCallerSigningKey(VECTORS.test_signing_key);
  const claims = { kind: "operator", email: "", method: "POST", target: "/chat" };
  assert.notEqual(mintCallerAssertion(signer, claims), mintCallerAssertion(signer, claims));
});

test("no key set means no signing", () => {
  assert.equal(parseCallerSigningKey(undefined), null);
  assert.equal(parseCallerSigningKey("   "), null);
});

test("a key that can't be used throws without echoing it", () => {
  const seed = VECTORS.test_signing_key.split(":")[1];
  for (const raw of [seed, `bad kid!:${seed}`, "k1:short", `:${seed}`, `k1:${seed}x`]) {
    assert.throws(
      () => parseCallerSigningKey(raw),
      (err) => err instanceof Error && !err.message.includes(seed),
    );
  }
});

test("a user is named by an email and the operator by no one", () => {
  const signer = parseCallerSigningKey(VECTORS.test_signing_key);
  assert.throws(() => mintCallerAssertion(signer, { kind: "user", email: "", method: "GET", target: "/" }));
  assert.throws(() =>
    mintCallerAssertion(signer, { kind: "operator", email: "a@b.example", method: "GET", target: "/" }),
  );
});

test("the proxy strips client caller headers before signing its own", () => {
  const route = readFileSync(new URL("../src/app/api/backend/[...path]/route.ts", import.meta.url), "utf8");
  // Covers x-caller-assertion: a client can never send one through.
  assert.match(route, /lower\.startsWith\("x-caller-"\)/);
  assert.match(route, /headers\.set\(\s*CALLER_ASSERTION_HEADER/);
  // Signed over exactly what fetch sends.
  assert.match(route, /target: `\$\{url\.pathname\}\$\{url\.search\}`/);
});
