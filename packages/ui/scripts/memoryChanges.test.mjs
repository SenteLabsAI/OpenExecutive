import assert from "node:assert/strict";
import test from "node:test";
import { changeCounts, changeDetail, changeHref, dayLabel } from "../src/lib/memoryChanges.ts";

const NOW = new Date(2026, 10, 3, 15, 0); // Tue Nov 3 2026, 3 pm local
const at = (...args) => new Date(...args).toISOString();
const change = (kind, extra = {}) => ({
  kind, id: 1, title: "x", source: "", status: "active", created_at: at(2026, 10, 3, 9, 0), can_undo: true, ...extra,
});

test("changeCounts names each kind, singular and plural", () => {
  assert.equal(changeCounts([change("lesson")]), "1 lesson");
  assert.equal(
    changeCounts([change("note"), change("fact"), change("fact"), change("worked_out")]),
    "2 facts · 1 figure · 1 note",
  );
  assert.equal(changeCounts([]), "");
});

test("changeDetail says what it is, where it came from and when", () => {
  assert.equal(changeDetail(change("lesson"), NOW), "Lesson · today");
  assert.equal(
    changeDetail(change("worked_out", { source: "the DCA statement", created_at: at(2026, 10, 2, 20, 0) }), NOW),
    "What it worked out · from the DCA statement · yesterday",
  );
  assert.equal(changeDetail(change("fact", { status: "proposed" }), NOW), "Fact · waiting for your OK · today");
});

test("dayLabel falls back to a date after yesterday", () => {
  assert.equal(dayLabel(at(2026, 10, 1, 12, 0), NOW), new Date(2026, 10, 1).toLocaleDateString(undefined, { month: "short", day: "numeric" }));
  assert.equal(dayLabel("not a time", NOW), "");
});

test("changeHref opens the list each kind lives in", () => {
  assert.equal(changeHref(change("decision")), "/memories?tab=corrections");
  assert.equal(changeHref(change("worked_out")), "/memories?tab=worked_out");
  assert.equal(changeHref(change("lesson")), "/memories?tab=reflection");
  assert.equal(changeHref(change("note")), "/memories?tab=history");
});
