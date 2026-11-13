// Chat's "Your browser is connected" card (lib/browserWelcome.ts).
import assert from "node:assert/strict";
import test from "node:test";

import { showBrowserWelcome } from "../src/lib/browserWelcome.ts";

const base = {
  landing: true,
  connected: true,
  starters: ["Open harborlane.com and tell me what you'd fix on the home page"],
  closed: false,
};

test("shows on a new chat once the browser is connected", () => {
  assert.equal(showBrowserWelcome(base), true);
});

test("never once used or closed", () => {
  assert.equal(showBrowserWelcome({ ...base, closed: true }), false);
});

test("not before it's connected, off a new chat, or with nothing to try", () => {
  assert.equal(showBrowserWelcome({ ...base, connected: false }), false);
  assert.equal(showBrowserWelcome({ ...base, landing: false }), false);
  assert.equal(showBrowserWelcome({ ...base, starters: [] }), false);
});
