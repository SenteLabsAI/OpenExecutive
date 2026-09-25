import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  FALSEY_ENV,
  isLoopbackHost,
  localOwnerModeEnabled,
  localOwnerSessionAllowed,
} from "../src/lib/localOwner.ts";

// `make dev` with Google sign-in not set up — the one configuration that opens
// one-person mode. Each test below changes one thing about it.
const MAKE_DEV = {
  devServer: true,
  flag: "1",
  googleClientId: "",
  publicDeployment: undefined,
};

// --- localOwnerModeEnabled -------------------------------------------------

test("make dev without Google sign-in turns one-person mode on", () => {
  assert.equal(localOwnerModeEnabled(MAKE_DEV), true);
});

test("a production build never runs one-person mode", () => {
  assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, devServer: false }), false);
});

test("without the flag make dev sets (plain npm run dev, Docker) it stays off", () => {
  assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, flag: undefined }), false);
  assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, flag: "" }), false);
  assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, flag: "true" }), false);
});

test("once Google sign-in is set up it is the only way in", () => {
  assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, googleClientId: "123.apps.googleusercontent.com" }), false);
  // Whitespace alone is not a client id.
  assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, googleClientId: "  " }), true);
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
    assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, publicDeployment: on }), false, on);
  }
  for (const off of ["", "0", "false", "No", "off"]) {
    assert.equal(localOwnerModeEnabled({ ...MAKE_DEV, publicDeployment: off }), true, off);
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

test("a missing Host header is refused", () => {
  assert.equal(isLoopbackHost(null), false);
  assert.equal(isLoopbackHost(undefined), false);
  assert.equal(isLoopbackHost(""), false);
});

// --- localOwnerSessionAllowed -----------------------------------------------

test("a one-person session works only while the mode is on and on this machine", () => {
  assert.equal(localOwnerSessionAllowed(true, "localhost:3000"), true);
  assert.equal(localOwnerSessionAllowed(true, "192.168.1.20:3000"), false);
  // Google sign-in was set up since: the old session stops working.
  assert.equal(localOwnerSessionAllowed(false, "localhost:3000"), false);
});
