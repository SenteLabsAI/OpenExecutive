import assert from "node:assert/strict";
import test from "node:test";
import { DeferredForget, learningStats, nowRight, toggleAll, unhelpfulLessonIds } from "../src/lib/learning.ts";

test("the card counts only what went right: live lessons, times used, misses now right", () => {
  const lessons = [
    { retired_at: null, shown: 3 },
    { retired_at: null, shown: 2 },
    { retired_at: "2026-10-20T00:00:00Z", shown: 9 },
  ];
  const misses = [{ replay_state: "closed" }, { replay_state: "open" }, { replay_state: "evidence" }];
  assert.deepEqual(learningStats(lessons, misses), { lessons: 2, uses: 5, nowRight: 1 });
  assert.deepEqual(learningStats([], []), { lessons: 0, uses: 0, nowRight: 0 });
});

test("Now gets right lists only the misses the nightly recheck passed", () => {
  const misses = [
    { id: 1, replay_state: "closed" },
    { id: 2, replay_state: "open" },
    { id: 3, replay_state: "evidence" },
    { id: 4, replay_state: "closed" },
  ];
  assert.deepEqual(nowRight(misses).map((m) => m.id), [1, 4]);
});

test("the quick pick: lessons that aren't helping", () => {
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
