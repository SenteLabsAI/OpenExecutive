import { createPrivateKey, randomBytes, sign, type KeyObject } from "node:crypto";

// Signed callers (docs/auth.md). The proxy signs who is signed in on every
// request it passes to the API, with an Ed25519 key only this server holds
// (CALLER_ASSERTION_PRIVATE_KEY), so the API checks a signature instead of
// taking x-caller-email from whoever holds BACKEND_SHARED_SECRET. The API side
// is packages/core/openexecutive/api/caller.py; scripts/callerAssertion.test.mjs
// fails if the two disagree on the constants or on a signed test vector.
//
// Server-only: it reads a private key. Never import it from a client component.

export const CALLER_ASSERTION_HEADER = "x-caller-assertion";
export const ASSERTION_VERSION = "v1";
export const ASSERTION_AUDIENCE = "openexecutive-api";
// Short, so a captured assertion is soon worthless, and well under the API's
// MAX_LIFETIME_S, so a slow clock on either side still leaves room.
export const ASSERTION_LIFETIME_S = 30;

const KID_RE = /^[A-Za-z0-9_-]{1,32}$/;
const SEED_RE = /^[A-Za-z0-9_-]{43}=?$/;
// The PKCS #8 wrapping of a raw 32-byte Ed25519 private key (RFC 8410).
const PKCS8_ED25519_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export type CallerSigner = { kid: string; key: KeyObject };

export type CallerClaims = {
  kind: "user" | "operator";
  // The signed-in person's email for a user; "" for the operator (local
  // login), who names no one and runs as the owner.
  email: string;
  method: string;
  // The path and query exactly as the proxy sends them upstream.
  target: string;
};

/**
 * CALLER_ASSERTION_PRIVATE_KEY (`kid:seed`, as scripts/make-caller-keys.py
 * prints it) → the signer, or null when it isn't set. Throws when it is set
 * but unusable; the message never includes the key.
 */
export function parseCallerSigningKey(raw: string | undefined): CallerSigner | null {
  const value = (raw ?? "").trim();
  if (!value) return null;
  const sep = value.indexOf(":");
  const kid = sep > 0 ? value.slice(0, sep).trim() : "";
  const seed = sep > 0 ? value.slice(sep + 1).trim() : "";
  if (!KID_RE.test(kid) || !SEED_RE.test(seed)) {
    throw new Error(
      "CALLER_ASSERTION_PRIVATE_KEY is <key id>:<private key>, as scripts/make-caller-keys.py prints it",
    );
  }
  const der = Buffer.concat([PKCS8_ED25519_PREFIX, Buffer.from(seed.replace(/=$/, ""), "base64url")]);
  return { kid, key: createPrivateKey({ key: der, format: "der", type: "pkcs8" }) };
}

/**
 * One `x-caller-assertion` value: good for this request only (its method, its
 * exact path and query, for ASSERTION_LIFETIME_S, once).
 */
export function mintCallerAssertion(
  signer: CallerSigner,
  claims: CallerClaims,
  nowMs: number = Date.now(),
  jti: string = randomBytes(18).toString("base64url"),
): string {
  const sub = claims.kind === "user" ? claims.email.trim().toLowerCase() : "";
  if (claims.kind === "user" && !sub.includes("@")) {
    throw new Error("a signed-in user is named by their email");
  }
  if (claims.kind === "operator" && claims.email.trim()) {
    throw new Error("the operator names no one");
  }
  const iat = Math.floor(nowMs / 1000);
  // Keys in sorted order and no spaces: byte for byte what
  // scripts/mint-caller-assertion.py signs, so the test vectors match.
  const payload = {
    aud: ASSERTION_AUDIENCE,
    exp: iat + ASSERTION_LIFETIME_S,
    iat,
    jti,
    kid: signer.kid,
    kind: claims.kind,
    m: claims.method.toUpperCase(),
    p: claims.target,
    sub,
  };
  const body = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  const signingInput = `${ASSERTION_VERSION}.${body}`;
  const signature = sign(null, Buffer.from(signingInput, "ascii"), signer.key).toString("base64url");
  return `${signingInput}.${signature}`;
}
