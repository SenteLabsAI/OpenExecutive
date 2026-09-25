import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { OWN_PAGE_FETCH_SITES, isCrossSiteWrite } from "../src/lib/crossSite.ts";

test("the app's own requests pass", () => {
  for (const method of ["POST", "PUT", "PATCH", "DELETE", "GET"]) {
    assert.equal(isCrossSiteWrite(method, "same-origin"), false, method);
  }
});

test("a form another localhost port posts is refused", () => {
  // Every localhost port is one "site", so the browser attaches the cookie
  // and reports same-site; other websites report cross-site.
  for (const site of ["same-site", "cross-site", "SAME-SITE"]) {
    for (const method of ["POST", "PUT", "PATCH", "DELETE", "post"]) {
      assert.equal(isCrossSiteWrite(method, site), true, `${method} ${site}`);
    }
  }
});

test("reads are never refused — another site cannot read the response anyway", () => {
  assert.equal(isCrossSiteWrite("GET", "cross-site"), false);
  assert.equal(isCrossSiteWrite("HEAD", "same-site"), false);
});

test("a typed URL, a bookmark or a non-browser client is not refused", () => {
  assert.equal(isCrossSiteWrite("POST", "none"), false);
  assert.equal(isCrossSiteWrite("POST", null), false);
});

test("the API's local-login check allows exactly the same Sec-Fetch-Site values", () => {
  const mainPy = readFileSync(new URL("../../core/openexecutive/api/main.py", import.meta.url), "utf8");
  const literal = /_OWN_PAGE_FETCH_SITES = frozenset\(\{([^}]*)\}\)/.exec(mainPy);
  assert.ok(literal, "_OWN_PAGE_FETCH_SITES not found in api/main.py");
  const apiValues = [...literal[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual([...OWN_PAGE_FETCH_SITES].sort(), apiValues.sort());
});
