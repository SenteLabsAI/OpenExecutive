import assert from "node:assert/strict";
import test from "node:test";
import { highlightParts, matchLabel, matchingMessages } from "../src/lib/chatSearch.ts";

test("every occurrence is marked, whatever its case", () => {
  assert.deepEqual(highlightParts("Runway is runway", "runway"), [
    { text: "Runway", match: true },
    { text: " is ", match: false },
    { text: "runway", match: true },
  ]);
  assert.deepEqual(highlightParts("burn 9% up", " 9% "), [
    { text: "burn ", match: false },
    { text: "9%", match: true },
    { text: " up", match: false },
  ]);
});

test("no query leaves the text whole", () => {
  assert.deepEqual(highlightParts("hello", "  "), [{ text: "hello", match: false }]);
  assert.deepEqual(highlightParts("", "x"), []);
});

test("result rows say how they matched", () => {
  assert.equal(matchLabel(undefined), null);
  assert.equal(matchLabel(0), "title match");
  assert.equal(matchLabel(1), "1 match");
  assert.equal(matchLabel(5), "5 matches");
});

test("an opened chat finds the messages that contain the words", () => {
  const messages = [
    { content: "How much runway?" },
    { content: "Payroll is $61k" },
    { content: [{ type: "image" }] },
    { content: "11 months of RUNWAY" },
  ];
  assert.deepEqual(matchingMessages(messages, "runway"), [0, 3]);
  assert.deepEqual(matchingMessages(messages, ""), []);
});
