import assert from "node:assert/strict";
import test from "node:test";
import { groupSessionsByDate } from "../src/lib/sessionGroups.ts";

const at = (session_id, updated_at) => ({
  session_id,
  title: session_id,
  created_at: updated_at,
  updated_at,
  message_count: 1,
});

// Local-time "now" so the day boundaries below line up with the grouping's
// own local-midnight arithmetic regardless of the machine's timezone.
const NOW = new Date(2026, 8, 24, 15, 0, 0);
const daysAgo = (d, hour = 12) =>
  new Date(2026, 8, 24 - d, hour, 0, 0).toISOString();

test("sessions land in Today / Yesterday / 7 / 30 / Older by local day", () => {
  const groups = groupSessionsByDate(
    [
      at("today", daysAgo(0, 0)),
      at("yesterday", daysAgo(1, 23)),
      at("week", daysAgo(7)),
      at("month", daysAgo(30)),
      at("old", daysAgo(31)),
    ],
    NOW,
  );
  assert.deepEqual(
    groups.map((g) => [g.key, g.items.map((i) => i.session_id)]),
    [
      ["today", ["today"]],
      ["yesterday", ["yesterday"]],
      ["prev7", ["week"]],
      ["prev30", ["month"]],
      ["older", ["old"]],
    ],
  );
});

test("empty buckets are dropped and in-bucket order is preserved", () => {
  const groups = groupSessionsByDate(
    [at("b", daysAgo(0, 14)), at("a", daysAgo(0, 9))],
    NOW,
  );
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].items.map((i) => i.session_id), ["b", "a"]);
});

test("an unparseable timestamp falls into Older instead of throwing", () => {
  const groups = groupSessionsByDate([at("bad", "not-a-date")], NOW);
  assert.deepEqual(groups.map((g) => g.key), ["older"]);
});

test("pinned chats come first in Pinned, whatever their date", () => {
  const pinned = (id, updated) => ({ ...at(id, updated), pinned_at: daysAgo(0) });
  const groups = groupSessionsByDate(
    [at("today", daysAgo(0)), pinned("old-pin", daysAgo(90)), pinned("new-pin", daysAgo(0))],
    NOW,
  );
  assert.deepEqual(
    groups.map((g) => [g.key, g.label, g.items.map((i) => i.session_id)]),
    [
      ["pinned", "Pinned", ["old-pin", "new-pin"]],
      ["today", "Today", ["today"]],
    ],
  );
});

test("topic view: pinned first, then topics in server order, one-offs under Other", async () => {
  const { groupSessionsByTopic } = await import("../src/lib/sessionGroups.ts");
  const chat = (session_id, extra = {}) => ({
    session_id,
    title: session_id,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    message_count: 2,
    ...extra,
  });
  const topics = [
    { id: 1, name: "Hiring a sales lead", chat_count: 2, kept: false },
    { id: 2, name: "Acme renewal", chat_count: 1, kept: false },
    { id: 3, name: "Board prep", chat_count: 1, kept: true },
  ];
  const groups = groupSessionsByTopic(
    [
      chat("pinned", { topic_id: 1, pinned_at: "2026-10-01T00:00:00Z" }),
      chat("hire-a", { topic_id: 1 }),
      chat("loose"),
      chat("acme", { topic_id: 2 }),
      chat("hire-b", { topic_id: 1 }),
      chat("board", { topic_id: 3 }),
      chat("gone", { topic_id: 99 }),
    ],
    topics,
  );
  assert.deepEqual(
    groups.map((g) => [g.label, g.items.map((s) => s.session_id)]),
    [
      ["Pinned", ["pinned"]],
      ["Hiring a sales lead", ["hire-a", "hire-b"]],
      ["Board prep", ["board"]],
      // A one-chat topic nobody kept, no topic, or an unknown one: Other, newest first.
      ["Other", ["loose", "acme", "gone"]],
    ],
  );
  assert.equal(groups[1].topic.id, 1);
  assert.equal(groups[3].topic, undefined);
});
