import assert from "node:assert/strict";
import test from "node:test";
import { initials } from "../src/lib/initials.ts";

test("initials take the first and last name's first letters", () => {
  assert.equal(initials("Jordan Avery"), "JA");
  assert.equal(initials("Mary Ann van der Berg"), "MB");
  assert.equal(initials("  dana  park "), "DP");
});

test("a single name gives its first two letters", () => {
  assert.equal(initials("Jordan"), "JO");
  assert.equal(initials("Ö"), "Ö");
});

test("an email uses its local part, split on dots, dashes and underscores", () => {
  assert.equal(initials("jordan.avery@example.com"), "JA");
  assert.equal(initials("jordan@example.com"), "JO");
  assert.equal(initials("j_avery@example.com"), "JA");
});

test("nothing usable gives a question mark", () => {
  assert.equal(initials(""), "?");
  assert.equal(initials("  "), "?");
});
