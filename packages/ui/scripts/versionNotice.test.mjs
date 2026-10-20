import assert from "node:assert/strict";
import test from "node:test";
import { UPGRADE_DOC_URL, versionNotice } from "../src/lib/versionNotice.ts";

const BASE = {
  current: "0.4.4",
  latest: "0.4.4",
  update_available: false,
  release_url: "https://github.com/SenteLabsAI/OpenExecutive/releases/tag/v0.4.4",
  check_enabled: true,
};

test("names the running version", () => {
  assert.equal(versionNotice(BASE).running, "Open Executive v0.4.4");
});

test("says when this is the latest release", () => {
  const n = versionNotice(BASE);
  assert.equal(n.status, "This is the latest release.");
  assert.equal(n.update, null);
});

test("links the release and the upgrade steps when a newer one is out", () => {
  const url = "https://github.com/SenteLabsAI/OpenExecutive/releases/tag/v0.5.0";
  const n = versionNotice({ ...BASE, latest: "0.5.0", update_available: true, release_url: url });
  assert.equal(n.status, "v0.5.0 is available.");
  assert.deepEqual(n.update, { releaseUrl: url, upgradeUrl: UPGRADE_DOC_URL });
});

test("a build ahead of the latest release offers no update", () => {
  const n = versionNotice({ ...BASE, current: "0.5.0-dev", latest: "0.4.4" });
  assert.equal(n.status, "The latest release is v0.4.4.");
  assert.equal(n.update, null);
});

test("an unreachable GitHub is said plainly", () => {
  const n = versionNotice({ ...BASE, latest: null, release_url: null });
  assert.match(n.status, /Couldn't reach GitHub/);
  assert.equal(n.update, null);
});

test("a turned-off check says so and still names the version", () => {
  const n = versionNotice({ ...BASE, latest: null, release_url: null, check_enabled: false });
  assert.equal(n.running, "Open Executive v0.4.4");
  assert.match(n.status, /turned off/);
  assert.equal(n.update, null);
});
