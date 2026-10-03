import assert from "node:assert/strict";
import test from "node:test";
import { domainBulkActions } from "../src/lib/reviewBulk.ts";

const shipped = (domain) => ({ domain, trusted_default: true, reviewed_at: null });
const upload = (domain) => ({ domain, trusted_default: false, reviewed_at: null });

test("a domain with shipped items and nothing queued offers Curate", () => {
  const actions = domainBulkActions("finance", [], { finance: 12 });
  assert.deepEqual(actions.map((a) => a.kind), ["curate-start"]);
  assert.equal(actions[0].label, "Curate finance (12)");
});

test("a curated domain offers Approve all pending and Stop curating", () => {
  const actions = domainBulkActions("hr", [shipped("hr"), shipped("hr")], { hr: 0 });
  assert.deepEqual(actions.map((a) => a.kind), ["approve", "curate-stop"]);
  assert.equal(actions[0].count, 2);
});

test("a user's own pending upload doesn't count toward Stop curating", () => {
  const actions = domainBulkActions("legal", [upload("legal")], { legal: 3 });
  assert.deepEqual(actions.map((a) => a.kind), ["approve", "curate-start"]);
});

test("other domains' items are ignored, and an empty domain offers nothing", () => {
  assert.deepEqual(domainBulkActions("sales", [shipped("hr")], {}), []);
});
