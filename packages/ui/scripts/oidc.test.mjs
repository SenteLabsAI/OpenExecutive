import assert from "node:assert/strict";
import test from "node:test";
import { OIDC_DEFAULT_NAME, emailVerified, issuerAllowed, oidcConfig } from "../src/lib/oidc.ts";

const KEYCLOAK = {
  issuer: "https://sso.acme.io/realms/acme",
  clientId: "open-executive",
  clientSecret: "kc-secret",
  name: undefined,
  trustUnverifiedEmail: undefined,
};

// --- oidcConfig ------------------------------------------------------------

test("issuer, client id and secret together turn SSO on", () => {
  const config = oidcConfig(KEYCLOAK);
  assert.deepEqual(config, {
    issuer: "https://sso.acme.io/realms/acme",
    clientId: "open-executive",
    clientSecret: "kc-secret",
    name: OIDC_DEFAULT_NAME,
    trustUnverifiedEmail: false,
  });
});

test("a half-filled block offers no SSO", () => {
  assert.equal(oidcConfig({ ...KEYCLOAK, issuer: undefined }), null);
  assert.equal(oidcConfig({ ...KEYCLOAK, clientId: " " }), null);
  assert.equal(oidcConfig({ ...KEYCLOAK, clientSecret: "" }), null);
});

test("the button name and values are trimmed", () => {
  const config = oidcConfig({ ...KEYCLOAK, issuer: " https://sso.acme.io/realms/acme ", name: " Okta " });
  assert.equal(config?.issuer, "https://sso.acme.io/realms/acme");
  assert.equal(config?.name, "Okta");
  assert.equal(oidcConfig({ ...KEYCLOAK, name: "  " })?.name, OIDC_DEFAULT_NAME);
});

test("only an explicit true trusts unverified emails", () => {
  for (const value of ["true", " TRUE ", "True"]) {
    assert.equal(oidcConfig({ ...KEYCLOAK, trustUnverifiedEmail: value })?.trustUnverifiedEmail, true, value);
  }
  for (const value of [undefined, "", "1", "yes", "false", "on"]) {
    assert.equal(oidcConfig({ ...KEYCLOAK, trustUnverifiedEmail: value })?.trustUnverifiedEmail, false, String(value));
  }
});

// --- issuerAllowed ---------------------------------------------------------

test("the issuer must be https, except on this machine", () => {
  assert.equal(issuerAllowed("https://sso.acme.io/realms/acme"), true);
  assert.equal(issuerAllowed("https://acme.auth0.com/"), true);
  for (const local of ["http://localhost:8080/realms/acme", "http://127.0.0.1:8080/r", "http://[::1]:8080/r"]) {
    assert.equal(issuerAllowed(local), true, local);
  }
  // Over plain http on a network, whoever answers could mint any verified email.
  for (const refused of [
    "http://keycloak:8080/realms/acme",
    "http://sso.acme.io/realms/acme",
    "http://localhost.acme.io/realms/acme",
    "ftp://sso.acme.io",
    "sso.acme.io/realms/acme",
    "not a url",
  ]) {
    assert.equal(issuerAllowed(refused), false, refused);
  }
});

test("a plain-http issuer on the network offers no SSO", () => {
  assert.equal(oidcConfig({ ...KEYCLOAK, issuer: "http://keycloak:8080/realms/acme" }), null);
  assert.equal(oidcConfig({ ...KEYCLOAK, issuer: "http://localhost:8080/realms/acme" })?.issuer, "http://localhost:8080/realms/acme");
});

// --- emailVerified ---------------------------------------------------------

const STRICT = { issuer: KEYCLOAK.issuer, trustUnverifiedEmail: false };

test("a verified email is trusted", () => {
  assert.equal(emailVerified({ email: "ada@acme.io", email_verified: true }, STRICT), true);
  // Some providers send the boolean as a string.
  assert.equal(emailVerified({ email: "ada@acme.io", email_verified: "true" }, STRICT), true);
});

test("an unverified, missing or odd claim fails closed", () => {
  for (const value of [false, "false", undefined, null, 1, "1", "yes", {}]) {
    assert.equal(emailVerified({ email: "ada@acme.io", email_verified: value }, STRICT), false, String(value));
  }
  assert.equal(emailVerified(null, STRICT), false);
  assert.equal(emailVerified(undefined, STRICT), false);
});

test("Entra's own verified claim counts only from an Entra issuer", () => {
  const entra = { issuer: "https://login.microsoftonline.com/0f1e2d3c-0000-4000-8000-000000000000/v2.0", trustUnverifiedEmail: false };
  assert.equal(emailVerified({ email: "ada@acme.io", xms_edov: true }, entra), true);
  assert.equal(emailVerified({ email: "ada@acme.io", xms_edov: false }, entra), false);
  assert.equal(emailVerified({ email: "ada@acme.io" }, entra), false);
  // Any other issuer may send whatever claims it likes; xms_edov means nothing there.
  assert.equal(emailVerified({ email: "ada@acme.io", xms_edov: true }, STRICT), false);
  const lookalike = { issuer: "https://login.microsoftonline.com.evil.io/t/v2.0", trustUnverifiedEmail: false };
  assert.equal(emailVerified({ email: "ada@acme.io", xms_edov: true }, lookalike), false);
});

test("the operator opt-in trusts any email the provider sends", () => {
  const trusting = { ...STRICT, trustUnverifiedEmail: true };
  assert.equal(emailVerified({ email: "ada@acme.io", email_verified: false }, trusting), true);
  assert.equal(emailVerified({ email: "ada@acme.io" }, trusting), true);
});
