import assert from "node:assert/strict";
import test from "node:test";
import {
  MEMORY_ACTIONS,
  MEMORY_LINE_MAX_CHARS,
  briefingMemoryLine,
  nudgeAction,
} from "../src/lib/briefing-memory.ts";

test("names the action, then the item on its own line — not the card body", () => {
  assert.equal(
    briefingMemoryLine(MEMORY_ACTIONS.artifact, "Harbor Point disposition plan"),
    'Let\'s discuss this flagged artifact.\n"Harbor Point disposition plan"',
  );
});

test("the action sentence ends before a question-shaped headline", () => {
  // Extraction accepts a quote only when "." follows it before any "?"; a
  // headline inside the sentence would void "I approve this proposal".
  const line = briefingMemoryLine(MEMORY_ACTIONS.approve, "Should we renew Acme at 3%?");
  assert.ok(line.startsWith("I approve this proposal.\n"));
});

test("collapses whitespace so a multi-line headline stays one line", () => {
  assert.equal(
    briefingMemoryLine(MEMORY_ACTIONS.approve, "  Renew\n\n the  lease "),
    'I approve this proposal.\n"Renew the lease"',
  );
});

test("clips long subjects well under the backend's 2000-char bound", () => {
  const line = briefingMemoryLine(MEMORY_ACTIONS.narrative, "x".repeat(5000));
  const quoted = line.slice(line.indexOf('"') + 1, line.lastIndexOf('"'));
  assert.equal(quoted.length, MEMORY_LINE_MAX_CHARS);
  assert.ok(quoted.endsWith("…"));
  assert.ok(line.length < 2000);
});

test("an empty subject still yields a non-empty line", () => {
  assert.equal(briefingMemoryLine(MEMORY_ACTIONS.proposal, "   "), "Tell me about this proposal.");
});

test("every action reads as the user speaking, never as a passive fact", () => {
  // "Asked to …" came back from peer memory as "<user> was asked to …".
  for (const action of [...Object.values(MEMORY_ACTIONS), nudgeAction("Sam")]) {
    assert.doesNotMatch(action, /^(Asked|Approved|Requested|Flagged)\b/, action);
    assert.match(action, /^(Let's|Help me|Tell me|I |Nudge )/, action);
  }
});

test("the nudge line names who to nudge", () => {
  assert.equal(
    briefingMemoryLine(nudgeAction("Sam"), "Lease renewal"),
    'Nudge Sam about this item.\n"Lease renewal"',
  );
});

test("clipping never splits an emoji into a lone surrogate", () => {
  const line = briefingMemoryLine(MEMORY_ACTIONS.proposal, "a".repeat(298) + "😀😀😀");
  assert.doesNotMatch(line, /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
  assert.ok(line.endsWith('…"'));
});
