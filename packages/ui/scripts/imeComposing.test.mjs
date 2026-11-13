import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { isImeComposing } from "../src/lib/imeComposing.ts";

test("an Enter mid-composition is the IME's, not a submit", () => {
  assert.equal(isImeComposing({ nativeEvent: { isComposing: true }, keyCode: 13 }), true);
  // Safari: the confirming keydown reports keyCode 229 with isComposing false.
  assert.equal(isImeComposing({ nativeEvent: { isComposing: false }, keyCode: 229 }), true);
  assert.equal(isImeComposing({ nativeEvent: { isComposing: false }, keyCode: 13 }), false);
  assert.equal(isImeComposing({}), false);
});

// Every Enter handler in the UI submits or confirms something, so each one has
// to ignore the Enter that ends an IME composition.
test("every Enter key handler checks isImeComposing", () => {
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.tsx?$/.test(name)) files.push(path);
    }
  };
  walk(fileURLToPath(new URL("../src", import.meta.url)));

  const missing = [];
  for (const path of files) {
    readFileSync(path, "utf8").split("\n").forEach((line, i) => {
      if (/\.key === "Enter"/.test(line) && !/isImeComposing\(/.test(line)) {
        missing.push(`${path.slice(path.indexOf("src"))}:${i + 1}`);
      }
    });
  }
  assert.deepEqual(missing, []);
});
