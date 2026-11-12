import assert from "node:assert/strict";
import test from "node:test";

import { safeCallbackUrl } from "../src/lib/callbackUrl.ts";

test("callbackUrl only ever leads to a path on this site", () => {
  assert.equal(safeCallbackUrl("/today?x=1"), "/today?x=1");
  assert.equal(safeCallbackUrl(undefined), "/");
  for (const bad of ["//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "/x\\y", "https://evil.example", "evil"]) {
    assert.equal(safeCallbackUrl(bad), "/", JSON.stringify(bad));
  }
});
