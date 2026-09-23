import assert from "node:assert/strict";
import test from "node:test";
import { MEMORY_LINE_MAX_CHARS, briefingMemoryLine } from "../src/lib/briefing-memory.ts";

test("names the action and the item, not the card body", () => {
  assert.equal(
    briefingMemoryLine("Asked to discuss the flagged artifact", "Harbor Point disposition plan"),
    'Asked to discuss the flagged artifact "Harbor Point disposition plan".',
  );
});

test("collapses whitespace so a multi-line headline stays one line", () => {
  assert.equal(
    briefingMemoryLine("Approved the proposal", "  Renew\n\n the  lease "),
    'Approved the proposal "Renew the lease".',
  );
});

test("clips long subjects well under the backend's 2000-char bound", () => {
  const line = briefingMemoryLine("Asked to dig into the briefing item", "x".repeat(5000));
  const quoted = line.slice(line.indexOf('"') + 1, line.lastIndexOf('"'));
  assert.equal(quoted.length, MEMORY_LINE_MAX_CHARS);
  assert.ok(quoted.endsWith("…"));
  assert.ok(line.length < 2000);
});

test("an empty subject still yields a non-empty line", () => {
  assert.equal(briefingMemoryLine("Asked about the proposal", "   "), "Asked about the proposal.");
});
