import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseAllowedEmails } from "../src/lib/allowlist.ts";
import {
  EXAMPLE_EMAIL_DOMAINS,
  formatAgo,
  isExampleEmail,
  safeInAppLink,
  signInCheck,
  sortChecks,
} from "../src/lib/setupStatus.ts";

const GOOGLE = {
  localLogin: false,
  googleClientId: "id.apps.googleusercontent.com",
  googleClientSecret: "google-secret",
  oidcIssuer: undefined,
  oidcClientId: undefined,
  oidcClientSecret: undefined,
  oidcTrustUnverifiedEmail: false,
  allowedEmails: parseAllowedEmails("ada@acme.io, bo@acme.io"),
  authUrl: "https://exec.acme.io",
  publicDeployment: true,
};

test("local login needs nothing else", () => {
  const check = signInCheck({ ...GOOGLE, localLogin: true, googleClientId: undefined, googleClientSecret: undefined });
  assert.equal(check.state, "ok");
  assert.match(check.summary, /Local login/);
});

test("Google sign-in with a real allow-list is green", () => {
  const check = signInCheck(GOOGLE);
  assert.equal(check.state, "ok");
  assert.match(check.summary, /the 2 addresses in ALLOWED_EMAILS/);
  assert.ok(!JSON.stringify(check).includes("google-secret"));
  const rosterOnly = signInCheck({ ...GOOGLE, allowedEmails: new Set() });
  assert.equal(rosterOnly.summary, "Google sign-in, for anyone with an email on the team list.");
});

test("half-configured Google sign-in is red", () => {
  assert.equal(signInCheck({ ...GOOGLE, googleClientSecret: " " }).state, "error");
  assert.equal(signInCheck({ ...GOOGLE, googleClientId: undefined }).state, "error");
});

const SSO_ONLY = {
  ...GOOGLE,
  googleClientId: undefined,
  googleClientSecret: undefined,
  oidcIssuer: "https://sso.acme.io/realms/acme",
  oidcClientId: "open-executive",
  oidcClientSecret: "kc-secret",
};

test("SSO sign-in alone, or next to Google, is green", () => {
  const ssoOnly = signInCheck(SSO_ONLY);
  assert.equal(ssoOnly.state, "ok");
  assert.match(ssoOnly.summary, /^SSO sign-in, for the 2 addresses/);
  assert.ok(!JSON.stringify(ssoOnly).includes("kc-secret"));
  const both = signInCheck({ ...SSO_ONLY, googleClientId: GOOGLE.googleClientId, googleClientSecret: GOOGLE.googleClientSecret });
  assert.equal(both.state, "ok");
  assert.match(both.summary, /^Google and SSO sign-in/);
});

test("half-configured SSO sign-in is red", () => {
  for (const missing of ["oidcIssuer", "oidcClientId", "oidcClientSecret"]) {
    const check = signInCheck({ ...SSO_ONLY, [missing]: " " });
    assert.equal(check.state, "error", missing);
    assert.match(check.summary, /AUTH_OIDC_ISSUER, AUTH_OIDC_ID and AUTH_OIDC_SECRET/);
  }
  // Even with Google working, a half-filled SSO block is named.
  assert.equal(signInCheck({ ...GOOGLE, oidcClientId: "open-executive" }).state, "error");
});

test("half-configured Google next to working SSO is still red", () => {
  assert.equal(signInCheck({ ...SSO_ONLY, googleClientId: "id.apps.googleusercontent.com" }).state, "error");
});

test("nothing set up asks for Google, as before", () => {
  const check = signInCheck({ ...GOOGLE, googleClientId: undefined, googleClientSecret: undefined });
  assert.equal(check.state, "error");
  assert.match(check.summary, /AUTH_GOOGLE_ID and AUTH_GOOGLE_SECRET/);
});

test("trusting unverified SSO emails is amber", () => {
  const check = signInCheck({ ...SSO_ONLY, oidcTrustUnverifiedEmail: true });
  assert.equal(check.state, "warn");
  assert.match(check.summary, /AUTH_OIDC_TRUST_UNVERIFIED_EMAIL/);
  // The opt-in means nothing while SSO is off.
  assert.equal(signInCheck({ ...GOOGLE, oidcTrustUnverifiedEmail: true }).state, "ok");
});

test("sample addresses left in ALLOWED_EMAILS are named", () => {
  const check = signInCheck({ ...GOOGLE, allowedEmails: parseAllowedEmails("ada@acme.io,alice@example.com") });
  assert.equal(check.state, "warn");
  assert.equal(check.summary, "ALLOWED_EMAILS still lists sample addresses: alice@example.com.");
});

test("a public deployment without AUTH_URL is amber", () => {
  assert.equal(signInCheck({ ...GOOGLE, authUrl: "" }).state, "warn");
  assert.equal(signInCheck({ ...GOOGLE, authUrl: "", publicDeployment: false }).state, "ok");
});

test("a fresh copy of .env.example is caught", () => {
  const example = readFileSync(new URL("../../../.env.example", import.meta.url), "utf8");
  const line = /^ALLOWED_EMAILS=(.*)$/m.exec(example);
  assert.ok(line, "ALLOWED_EMAILS not found in .env.example");
  const samples = parseAllowedEmails(line[1]);
  assert.ok(samples.size > 0);
  for (const email of samples) assert.ok(isExampleEmail(email), email);
});

test("the API flags exactly the same sample email domains", () => {
  const py = readFileSync(new URL("../../core/openexecutive/api/setup_checks.py", import.meta.url), "utf8");
  const literal = /_EXAMPLE_EMAIL_DOMAINS = frozenset\(\{([^}]*)\}\)/.exec(py);
  assert.ok(literal, "_EXAMPLE_EMAIL_DOMAINS not found in api/setup_checks.py");
  const apiValues = [...literal[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual([...EXAMPLE_EMAIL_DOMAINS].sort(), apiValues.sort());
});

test("worst lights come first, otherwise in arrival order", () => {
  const light = (id, state) => ({ id, label: id, state, summary: "", fix: null, link: null, last_activity: null });
  const sorted = sortChecks([light("a", "ok"), light("b", "off"), light("c", "warn"), light("d", "error"), light("e", "ok")]);
  assert.deepEqual(
    sorted.map((c) => c.id),
    ["d", "c", "a", "e", "b"],
  );
});

test("formatAgo", () => {
  const now = new Date("2026-09-25T12:00:00Z");
  assert.equal(formatAgo("2026-09-25T11:59:30Z", now), "less than a minute ago");
  assert.equal(formatAgo("2026-09-25T11:59:00Z", now), "1 minute ago");
  assert.equal(formatAgo("2026-09-25T11:15:00Z", now), "45 minutes ago");
  assert.equal(formatAgo("2026-09-25T10:00:00Z", now), "2 hours ago");
  assert.equal(formatAgo("2026-09-21T12:00:00Z", now), "4 days ago");
  assert.equal(formatAgo("2026-09-25T11:50:00.000000Z", now), "10 minutes ago");
  assert.equal(formatAgo("not a time", now), "at an unknown time");
});

test("only in-app paths become links", () => {
  assert.equal(safeInAppLink("/people"), "/people");
  assert.equal(safeInAppLink("/onboard"), "/onboard");
  for (const bad of [null, "", "//evil.example", "https://evil.example", "javascript:alert(1)", "/people?next=//x", "people"]) {
    assert.equal(safeInAppLink(bad), null, String(bad));
  }
});
