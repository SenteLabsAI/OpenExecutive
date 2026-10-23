import assert from "node:assert/strict";
import test from "node:test";
import { DeferredForget, fixedMissIds, groupByTopic, isoWeek, learningStats, missTopic, toggleAll, unhelpfulLessonIds } from "../src/lib/learning.ts";

test("isoWeek matches Python's isocalendar, across a year end", () => {
  assert.equal(isoWeek(new Date("2026-10-19T09:00:00Z")), "2026-W43");
  assert.equal(isoWeek(new Date("2026-10-18T23:00:00Z")), "2026-W42");
  assert.equal(isoWeek(new Date("2027-01-01T12:00:00Z")), "2026-W53");
  assert.equal(isoWeek(new Date("2024-12-30T12:00:00Z")), "2025-W01");
});

test("the card counts this week only, and lessons and replays as given", () => {
  const weeks = [
    { week: "2026-W43", graded: 14, misses: 2 },
    { week: "2026-W42", graded: 30, misses: 5 },
  ];
  const now = new Date("2026-10-19T09:00:00Z");
  assert.deepEqual(learningStats(weeks, 1, { misses: 2, would_pass: 1 }, now), {
    checked: 14,
    fellShort: 2,
    lessons: 1,
    wouldGoRight: { pass: 1, of: 2 },
  });
});

test("a quiet week and no misses read as zero and nothing to check", () => {
  const now = new Date("2026-11-02T09:00:00Z");
  const stats = learningStats([{ week: "2026-W43", graded: 14, misses: 2 }], 0, { misses: 0, would_pass: 0 }, now);
  assert.equal(stats.checked, 0);
  assert.equal(stats.fellShort, 0);
  assert.equal(stats.wouldGoRight, null);
});

test("misses group by cause in a fixed order, falling back to their kind", () => {
  const misses = [
    { id: 1, kind: "product", cause: "tool" },
    { id: 2, kind: "company", cause: "term" },
    { id: 3, kind: "product", cause: null },
    { id: 4, kind: "company" },
    { id: 5, kind: "company", cause: "fact" },
    { id: 6, kind: "company", cause: "term" },
  ];
  assert.deepEqual(
    groupByTopic(misses).map((g) => [g.topic, g.items.map((m) => m.id)]),
    [["term", [2, 6]], ["fact", [5]], ["company", [4]], ["tool", [1]], ["product", [3]]],
  );
  assert.deepEqual(groupByTopic([]), []);
});

test("a cause that doesn't belong to the miss's kind files it under the kind", () => {
  assert.equal(missTopic({ kind: "company", cause: "tool" }), "company");
  assert.equal(missTopic({ kind: "product", cause: "cant" }), "cant");
  assert.equal(missTopic({ kind: "product", cause: "" }), "product");
});

test("the quick picks: misses that would go right today, lessons that aren't helping", () => {
  assert.deepEqual(
    fixedMissIds([
      { id: 1, replay_state: "closed" },
      { id: 2, replay_state: "open" },
      { id: 3, replay_state: "evidence" },
      { id: 4, replay_state: "closed" },
    ]),
    [1, 4],
  );
  const lesson = (id, fine_after, miss_after, retired_at = null) => ({ id, fine_after, miss_after, retired_at });
  assert.deepEqual(
    unhelpfulLessonIds([lesson(1, 1, 2), lesson(2, 2, 2), lesson(3, 0, 0), lesson(4, 0, 3, "2026-10-01T00:00:00Z")]),
    [1],
  );
});

test("select all adds the rest, and takes them all off once all are on", () => {
  const some = toggleAll(new Set([1]), [1, 2, 3]);
  assert.deepEqual([...some].sort(), [1, 2, 3]);
  const none = toggleAll(new Set([1, 2, 3, 9]), [1, 2, 3]);
  assert.deepEqual([...none], [9]);
  assert.deepEqual([...toggleAll(new Set(), [])], []);
});

function recorder(fail = () => false) {
  const sent = [];
  const reports = [];
  const queue = new DeferredForget(
    async (item, keepalive) => {
      sent.push([item, keepalive]);
      if (fail(item)) throw new Error("no");
    },
    6000,
  );
  const onSent = (items, failed) => reports.push([items, failed]);
  return { queue, onSent, sent, reports };
}

const settle = () => new Promise((r) => setImmediate(r));

test("a forget waits for its window, then sends each item once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { queue, onSent, sent, reports } = recorder();
  queue.hold([1, 2], onSent);
  assert.deepEqual(queue.waiting, [1, 2]);
  t.mock.timers.tick(5999);
  assert.deepEqual(sent, []);
  t.mock.timers.tick(1);
  await settle();
  assert.deepEqual(sent, [[1, false], [2, false]]);
  assert.deepEqual(reports, [[[1, 2], 0]]);
  t.mock.timers.tick(10000);
  await settle();
  assert.equal(sent.length, 2);
});

test("undo takes the batch back and nothing is sent", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { queue, onSent, sent } = recorder();
  queue.hold([7], onSent);
  assert.deepEqual(queue.undo(), [7]);
  assert.deepEqual(queue.undo(), []);
  t.mock.timers.tick(10000);
  await settle();
  assert.deepEqual(sent, []);
});

test("a second forget sends the first at once; a flush sends with keepalive", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { queue, onSent, sent, reports } = recorder((item) => item === 2);
  queue.hold([1, 2], onSent);
  queue.hold([3], onSent);
  await settle();
  assert.deepEqual(sent, [[1, false], [2, false]]);
  assert.deepEqual(reports, [[[1, 2], 1]]);
  assert.deepEqual(queue.waiting, [3]);
  await queue.flush({ keepalive: true });
  assert.deepEqual(sent.at(-1), [3, true]);
  t.mock.timers.tick(10000);
  await settle();
  assert.equal(sent.length, 3);
});
