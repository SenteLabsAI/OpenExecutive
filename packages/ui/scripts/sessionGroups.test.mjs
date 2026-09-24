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
