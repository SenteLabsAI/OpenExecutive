import assert from "node:assert/strict";
import test from "node:test";
import { loadsInline } from "../src/lib/markdownImages.ts";

test("an image this app serves loads inline", () => {
  assert.equal(loadsInline("/brand/logo.png"), true);
  assert.equal(loadsInline("  /api/artifacts/1/chart.png"), true);
});

test("an image from anywhere else does not", () => {
  assert.equal(loadsInline("https://attacker.example/b.png?d=secret"), false);
  assert.equal(loadsInline("http://attacker.example/b.png"), false);
  assert.equal(loadsInline("//attacker.example/b.png"), false);
  assert.equal(loadsInline("/\\attacker.example/b.png"), false);
  assert.equal(loadsInline("b.png"), false);
  assert.equal(loadsInline(""), false);
  assert.equal(loadsInline(undefined), false);
});
