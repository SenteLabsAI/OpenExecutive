import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ageAnchor,
  approveLabel,
  askerName,
  cardButtons,
  clearAll,
  clearNotice,
  groupByRequest,
  isOlder,
  isSignalCard,
  newestAnchor,
  splitOlder,
  waitedOnFirst,
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

test("Clear all leaves kept cards in view and only clears the older ones", async () => {
  const closed = [];
  const card = (id, at, extra = {}) => ({
    id, at, ...extra,
    clear: async () => {
      if (extra.fails) throw new Error("server said no");
      closed.push(id);
    },
  });
  const queue = [
    card("fresh", daysAgo(1)),
    card("sending-reply", daysAgo(5), { keep: true }),
    card("roster", daysAgo(9), { keep: true }),
    card("old-alert", daysAgo(4)),
    card("old-decision", daysAgo(6), { fails: true }),
  ];
  const { current, older } = splitOlder(queue, (c) => c.at, NOW, (c) => Boolean(c.keep));
  assert.deepEqual(current.map((c) => c.id), ["fresh", "sending-reply", "roster"]);

  const { cleared, failed } = await clearAll(older);
  assert.deepEqual(closed, ["old-alert"]);
  assert.deepEqual(cleared.map((c) => c.id), ["old-alert"]);
  // The one that failed is reported, so it stays listed with a notice.
  assert.deepEqual(failed.map((c) => c.id), ["old-decision"]);
  assert.equal(clearNotice(failed.length, older.length), "1 of 2 didn't clear. They're still under Older. Try again.");
  assert.equal(clearNotice(0, 2), null);
});

test("a card someone else is waiting on goes first, each part in its order", () => {
  const items = [{ id: 1 }, { id: 2, waited: true }, { id: 3 }, { id: 4, waited: true }];
  assert.deepEqual(waitedOnFirst(items).map((i) => i.id), [2, 4, 1, 3]);
  assert.deepEqual(waitedOnFirst([]), []);
});

test("a signal from outside says Got it unless it needs an OK", () => {
  const ext = isSignalCard({ category: "alert", topic_tags: ["external:app"] });
  assert.equal(ext, true);
  assert.deepEqual(cardButtons("", ext), { noApprove: true, gotIt: true });
  assert.deepEqual(cardButtons("ok", ext), { noApprove: false, gotIt: false });
  assert.equal(isSignalCard({ category: "alert", surfaced_reason: "moved 8%" }), true);
  assert.equal(isSignalCard({ category: "alert", topic_tags: [] }), false);
  assert.deepEqual(cardButtons("", false), { noApprove: false, gotIt: false });
  assert.deepEqual(cardButtons("fyi", false), { noApprove: true, gotIt: true });
});

test("the asked chip names the first name, past a title", () => {
  assert.equal(askerName("Sam Okafor"), "Sam");
  assert.equal(askerName("Dr. Sam Okafor"), "Sam");
  assert.equal(askerName("Someone on your team"), "Someone on your team");
});
