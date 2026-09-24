import { strict as assert } from "node:assert";
import test from "node:test";

import { CLIENT_TURN_ID_RE, newClientTurnId } from "../src/lib/turn-id.ts";

test("minted ids match the regex the server enforces", () => {
  for (let i = 0; i < 50; i++) {
    const id = newClientTurnId();
    assert.match(id, CLIENT_TURN_ID_RE, `minted id rejected by server regex: ${id}`);
  }
});

test("ids are unique", () => {
  const seen = new Set();
  for (let i = 0; i < 200; i++) seen.add(newClientTurnId());
  assert.equal(seen.size, 200);
});

// `globalThis.crypto` is a getter-only accessor in Node, so it can only be
// swapped via defineProperty.
function withCrypto(stub, fn) {
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  Object.defineProperty(globalThis, "crypto", {
    value: stub,
    configurable: true,
    writable: true,
  });
  try {
    fn();
  } finally {
    Object.defineProperty(globalThis, "crypto", original);
  }
}

test("getRandomValues fallback still matches the server regex", () => {
  const real = globalThis.crypto;
  // Simulate a non-secure context: getRandomValues present, randomUUID gone.
  withCrypto({ getRandomValues: (a) => real.getRandomValues(a) }, () => {
    const id = newClientTurnId();
    assert.match(id, CLIENT_TURN_ID_RE);
    assert.equal(id.length, 32);
  });
});

test("last-resort fallback still matches the server regex", () => {
  withCrypto(undefined, () => {
    assert.match(newClientTurnId(), CLIENT_TURN_ID_RE);
  });
});
