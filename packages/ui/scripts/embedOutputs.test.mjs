// The build outputs for host apps (scripts/embed-outputs.mjs).
import assert from "node:assert/strict";
import test from "node:test";
import { isAppPath } from "../src/lib/embed.ts";
import { embedManifest, hostList } from "./embed-outputs.mjs";

test("the manifest names every destination and hub tab by a route of this app", () => {
  const manifest = embedManifest();
  assert.equal(manifest.protocol, "oe-embed/v1");
  assert.equal(manifest.contract, 1);
  for (const mode of ["team", "solo"]) {
    const { destinations, hubs } = manifest.navigation[mode];
    assert.ok(destinations.length >= 5, mode);
    for (const href of [...destinations.map((d) => d.href), ...hubs.flatMap((h) => h.tabs.map((t) => t.href))]) {
      assert.ok(isAppPath(href.split(/[?#]/)[0]), `${mode}: ${href}`);
    }
  }
});

test("the host list goes out as it is", () => {
  const list = hostList();
  assert.ok(Array.isArray(list.calls) && list.calls.length > 0);
});
