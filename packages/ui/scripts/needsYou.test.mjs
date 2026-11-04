import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ageAnchor,
  approveLabel,
  groupByRequest,
  isOlder,
  newestAnchor,
  splitOlder,
} from "../src/lib/needsYou.ts";

const NOW = new Date("2026-11-04T16:00:00Z");
const daysAgo = (d) => new Date(NOW.getTime() - d * 86400000).toISOString();

test("a card three days untouched is older; one that re-fired is not", () => {
  assert.equal(isOlder(daysAgo(3), NOW), true);
  assert.equal(isOlder(daysAgo(2.9), NOW), false);
  assert.equal(isOlder("not a time", NOW), false);
  assert.equal(ageAnchor({ created_at: daysAgo(5), last_seen_at: daysAgo(1) }), daysAgo(1));
  assert.equal(ageAnchor({ created_at: daysAgo(5), last_seen_at: null }), daysAgo(5));
});

test("splitOlder keeps order and leaves kept cards in view", () => {
  const items = [
    { id: 1, at: daysAgo(4) },
    { id: 2, at: daysAgo(1) },
    { id: 3, at: daysAgo(6), keep: true },
    { id: 4, at: daysAgo(10) },
  ];
  const { current, older } = splitOlder(items, (i) => i.at, NOW, (i) => Boolean(i.keep));
  assert.deepEqual(current.map((i) => i.id), [2, 3]);
  assert.deepEqual(older.map((i) => i.id), [1, 4]);
});

test("groupByRequest gathers a request's cards where the first stood", () => {
  const groups = groupByRequest([
    { id: 1, group_key: "a" },
    { id: 2 },
    { id: 3, group_key: "a" },
    { id: 4, group_key: "b" },
    { id: 5, group_key: "a" },
  ]);
  assert.deepEqual(
    groups.map((g) => g.map((p) => p.id)),
    [[1, 3, 5], [2], [4]],
  );
});

test("a group is as new as its newest card", () => {
  assert.equal(
    newestAnchor([{ created_at: daysAgo(5) }, { created_at: daysAgo(2) }, { created_at: "bad" }]),
    daysAgo(2),
  );
  assert.equal(approveLabel(3), "Approve 3");
  assert.equal(approveLabel(1), "Approve");
});
