import assert from "node:assert/strict";
import test from "node:test";
import { fadeMask } from "../src/components/ui/useScrollFade.ts";

// A tab row that scrolls sideways fades at an edge with more past it.
test("no fade when the row fits", () => {
  assert.equal(fadeMask(false, false), undefined);
});

test("fades the right edge while more tabs are past it", () => {
  assert.equal(fadeMask(false, true), "linear-gradient(to right, black, black calc(100% - 2.5rem), transparent)");
});

test("fades the left edge once scrolled, and both in the middle", () => {
  assert.equal(fadeMask(true, false), "linear-gradient(to right, transparent, black 2.5rem, black)");
  assert.equal(
    fadeMask(true, true),
    "linear-gradient(to right, transparent, black 2.5rem, black calc(100% - 2.5rem), transparent)",
  );
});
