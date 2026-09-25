import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  FALSEY_ENV,
  isLoopbackHost,
  localLoginEnabled,
  localLoginSessionAllowed,
} from "../src/lib/localLogin.ts";

// `make dev` with Google sign-in not set up — the one configuration that opens
// local login. Each test below changes one thing about it.
const MAKE_DEV = {
  devServer: true,
  flag: "1",
  googleClientId: "",
  publicDeployment: undefined,
};

// --- localLoginEnabled -------------------------------------------------

test("make dev without Google sign-in turns local login on", () => {
  assert.equal(localLoginEnabled(MAKE_DEV), true);
});

test("a production build never runs local login", () => {
  assert.equal(localLoginEnabled({ ...MAKE_DEV, devServer: false }), false);
});

test("without the flag make dev sets (plain npm run dev, Docker) it stays off", () => {
  assert.equal(localLoginEnabled({ ...MAKE_DEV, flag: undefined }), false);
  assert.equal(localLoginEnabled({ ...MAKE_DEV, flag: "" }), false);
  assert.equal(localLoginEnabled({ ...MAKE_DEV, flag: "true" }), false);
});

test("once Google sign-in is set up it is the only way in", () => {
  assert.equal(localLoginEnabled({ ...MAKE_DEV, googleClientId: "123.apps.googleusercontent.com" }), false);
  // Whitespace alone is not a client id.
  assert.equal(localLoginEnabled({ ...MAKE_DEV, googleClientId: "  " }), true);
});

test("OE_PUBLIC_DEPLOYMENT's off-values match the API's exactly", () => {
  // If the UI counted a value as "off" that the API counts as "on", an
  // internet-facing API could sit behind a UI that opens without sign-in.
  const mainPy = readFileSync(new URL("../../core/openexecutive/api/main.py", import.meta.url), "utf8");
  const literal = /_FALSEY_ENV = frozenset\(\{([^}]*)\}\)/.exec(mainPy);
  assert.ok(literal, "_FALSEY_ENV not found in api/main.py");
  const apiValues = [...literal[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual([...FALSEY_ENV].sort(), apiValues.sort());
});

test("a public deployment always requires sign-in, read like the API reads it", () => {
  for (const on of ["1", "true", "YES", " on "]) {
    assert.equal(localLoginEnabled({ ...MAKE_DEV, publicDeployment: on }), false, on);
  }
  for (const off of ["", "0", "false", "No", "off"]) {
    assert.equal(localLoginEnabled({ ...MAKE_DEV, publicDeployment: off }), true, off);
  }
});

// --- isLoopbackHost ---------------------------------------------------------

test("loopback names are accepted with or without a port", () => {
  for (const host of ["localhost", "localhost:3000", "LOCALHOST:3000", "127.0.0.1", "127.0.0.1:3000", "[::1]", "[::1]:3000"]) {
    assert.equal(isLoopbackHost(host), true, host);
  }
});

test("other machines and look-alike names are refused", () => {
  for (const host of [
    "192.168.1.20:3000", // the same laptop, reached over the LAN
    "exec.example.com",
    "localhost.example.com", // DNS rebinding keeps the attacker's own name
    "127.0.0.1.nip.io",
    "evil.com:3000",
    "localhost:3000@evil.com",
    "localhost:abc",
    "[::1]x",
    "::1", // an IPv6 Host header must be bracketed
    "[::ffff:192.168.1.20]:3000",
  ]) {
    assert.equal(isLoopbackHost(host), false, host);
  }
});

test("the API's loopback rule accepts exactly the hosts the UI's does", () => {
  // With local login both apps gate on "addressed to this computer". The API
  // keeps its own regex (api/main.py _LOOPBACK_HOST_RE, used with fullmatch
  // after strip); this runs one corpus through both so they cannot drift.
  const mainPy = readFileSync(new URL("../../core/openexecutive/api/main.py", import.meta.url), "utf8");
  const literal = /_LOOPBACK_HOST_RE = re\.compile\(r"([^"]+)", re\.IGNORECASE\)/.exec(mainPy);
  assert.ok(literal, "_LOOPBACK_HOST_RE not found in api/main.py");
  const apiRule = new RegExp(`^(?:${literal[1]})$`, "i");
  for (const host of [
    "localhost", "localhost:3000", "LOCALHOST:8000", "127.0.0.1", "127.0.0.1:8000", "[::1]", "[::1]:8000",
    " localhost:3000 ", "localhost:", "localhost:123456", "localhost.", "localhost.example.com",
    "127.0.0.1.nip.io", "127.0.0.2", "0.0.0.0", "::1", "[::ffff:127.0.0.1]", "192.168.1.20:3000",
    "evil.com", "localhost:3000@evil.com", "",
  ]) {
    assert.equal(apiRule.test(host.trim()), isLoopbackHost(host), JSON.stringify(host));
  }
});

test("a missing Host header is refused", () => {
  assert.equal(isLoopbackHost(null), false);
  assert.equal(isLoopbackHost(undefined), false);
  assert.equal(isLoopbackHost(""), false);
});

// --- localLoginSessionAllowed -----------------------------------------------

test("a local-login session works only while it is on and on this machine", () => {
  assert.equal(localLoginSessionAllowed(true, "localhost:3000"), true);
  assert.equal(localLoginSessionAllowed(true, "192.168.1.20:3000"), false);
  // Google sign-in was set up since: the old session stops working.
  assert.equal(localLoginSessionAllowed(false, "localhost:3000"), false);
});
