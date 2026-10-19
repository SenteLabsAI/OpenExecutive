import assert from "node:assert/strict";
import test from "node:test";

import { GROUP_OF, GROUP_ORDER, changedRows, groupTitle, rowId, snapshot } from "../src/lib/whatItDoes.ts";

const row = (id, outcome, what = "Replies to people you know.", why = "") => ({
  id, what, outcome, then: "", why, setting: "handle_it", items: [],
});
const section = (key, rows) => ({
  key, title: "", available: true, unavailable: "", notes: [], rows, footer: "", goes: 0,
});

test("every outcome lands in a group that is shown", () => {
  for (const outcome of ["goes", "waits", "always_waits", "drafts", "off"]) {
    assert.ok(GROUP_ORDER.includes(GROUP_OF[outcome]), outcome);
  }
  assert.equal(GROUP_OF.always_waits, "asks");
  assert.equal(groupTitle("goes", "as_you"), "Sends on its own");
  assert.equal(groupTitle("goes", "departments"), "Acts on its own");
  assert.equal(groupTitle("asks", "executive"), "Asks first");
});

test("nothing is marked on the first load", () => {
  const data = { paused: false, sections: [section("as_you", [row("replies_known", "goes")])] };
  assert.equal(changedRows(null, snapshot(data)).size, 0);
});

test("a row that changes, or appears, is marked by section", () => {
  const before = snapshot({
    paused: false,
    sections: [section("as_you", [row("rules", "always_waits"), row("replies_known", "waits")]), section("executive", [row("rules", "always_waits")])],
  });
  const after = snapshot({
    paused: false,
    sections: [
      section("as_you", [row("rules", "always_waits", "Replies that match your rules."), row("replies_known", "waits"), row("links", "goes")]),
      section("executive", [row("rules", "always_waits")]),
    ],
  });
  const changed = changedRows(before, after);
  assert.deepEqual([...changed].sort(), ["as_you:links", "as_you:rules"]);
  assert.ok(!changed.has(rowId("executive", row("rules", "always_waits"))));
});
