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

test("topic layout: areas over related topics, bands by last chat, Other last", async () => {
  const { layoutTopics } = await import("../src/lib/sessionGroups.ts");
  const now = new Date(2026, 9, 17, 12, 0, 0);
  const daysAgo = (n) => new Date(2026, 9, 17 - n, 9, 0, 0).toISOString();
  const chat = (session_id, updated, extra = {}) => ({
    session_id,
    title: session_id,
    created_at: updated,
    updated_at: updated,
    message_count: 2,
    ...extra,
  });
  const topics = [
    { id: 1, name: "St. Albans Disposition", chat_count: 2, kept: false, area: "St. Albans" },
    { id: 2, name: "St. Albans Offers", chat_count: 2, kept: false, area: "St. Albans" },
    { id: 3, name: "CAHEC Close", chat_count: 2, kept: false },
    { id: 4, name: "Estes Park", chat_count: 2, kept: false, area: "Colorado" },
    { id: 5, name: "Insurance", chat_count: 2, kept: false },
    { id: 6, name: "Old pinned", chat_count: 2, kept: true, pinned_at: daysAgo(40) },
  ];
  const sessions = [
    chat("d1", daysAgo(0), { topic_id: 1 }),
    chat("o1", daysAgo(1), { topic_id: 2 }),
    chat("c1", daysAgo(2), { topic_id: 3 }),
    chat("d2", daysAgo(3), { topic_id: 1 }),
    chat("c2", daysAgo(4), { topic_id: 3 }),
    chat("o2", daysAgo(5), { topic_id: 2 }),
    chat("e1", daysAgo(9), { topic_id: 4 }),
    chat("e2", daysAgo(10), { topic_id: 4 }),
    chat("i1", daysAgo(45), { topic_id: 5 }),
    chat("i2", daysAgo(50), { topic_id: 5 }),
    chat("p1", daysAgo(60), { topic_id: 6 }),
    chat("p2", daysAgo(61), { topic_id: 6 }),
    chat("loose", daysAgo(2)),
  ];
  const layout = layoutTopics(sessions, topics, now);
  assert.deepEqual(
    layout.bands.map((b) => [b.label, b.entries.map((e) => e.label)]),
    [
      // A pinned topic stays up top, whatever its age.
      ["Active this week", ["Old pinned", "St. Albans", "CAHEC Close"]],
      // An area with one topic left is just that topic.
      ["Quiet for a week", ["Estes Park"]],
      ["Quiet for a month or more", ["Insurance"]],
    ],
  );
  const area = layout.bands[0].entries[1];
  assert.equal(area.area, "St. Albans");
  // Under the area, a topic drops the area's name it starts with.
  assert.deepEqual(area.children.map((g) => g.label), ["Disposition", "Offers"]);
  assert.equal(area.children[0].topic.name, "St. Albans Disposition");
  assert.deepEqual(area.items.map((s) => s.session_id), ["d1", "o1", "d2", "o2"]);
  assert.deepEqual(layout.other.items.map((s) => s.session_id), ["loose"]);
});

test("activeLabel says Today, Yesterday, a weekday, then the date", async () => {
  const { activeLabel } = await import("../src/lib/sessionGroups.ts");
  const now = new Date(2026, 9, 17, 12, 0, 0);
  assert.equal(activeLabel(new Date(2026, 9, 17, 8).toISOString(), now), "Today");
  assert.equal(activeLabel(new Date(2026, 9, 16, 23).toISOString(), now), "Yesterday");
  assert.equal(
    activeLabel(new Date(2026, 9, 13, 9).toISOString(), now),
    new Date(2026, 9, 13).toLocaleDateString(undefined, { weekday: "short" }),
  );
  assert.equal(activeLabel("", now), "");
});
