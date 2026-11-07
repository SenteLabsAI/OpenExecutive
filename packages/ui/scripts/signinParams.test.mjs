import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const page = readFileSync(new URL("../src/app/signin/page.tsx", import.meta.url), "utf8");

test("a repeated query parameter takes its first value instead of crashing the page", async () => {
  // A link with callbackUrl twice reached safeCallbackUrl as an array and
  // threw "startsWith is not a function".
  const { firstParam } = await import("../src/lib/searchParam.ts");
  assert.equal(firstParam(["/a", "/"]), "/a");
  assert.equal(firstParam("/a"), "/a");
  assert.equal(firstParam(undefined), undefined);
  for (const name of ["callbackUrl", "error"]) {
    assert.match(page, new RegExp(`= firstParam\\(query\\.${name}\\);`));
  }
  assert.doesNotMatch(page, /\} = await searchParams;/);
});

test("Google sign-in always shows the account picker", () => {
  assert.match(page, /signIn\("google", \{ redirectTo: safeDest \}, \{ prompt: "select_account" \}\)/);
});
