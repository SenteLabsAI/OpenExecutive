import assert from "node:assert/strict";
import test from "node:test";
import { addPhrase, markNew, styleSentences } from "../src/lib/voiceStyle.ts";

const empty = { greetings: {}, sign_off: "", length: "", formality: "", habits: [], avoid: [] };

test("a style reads as plain sentences", () => {
  assert.deepEqual(
    styleSentences({
      greetings: { team: "Hi {first},", other: "Hello {first}," },
      sign_off: "Cheers,\nSam",
      length: "short",
      formality: "casual",
      habits: ["gets to the point in the first line"],
      avoid: ["Never uses exclamation marks"],
    }),
    [
      "Usually short, casual emails.",
      'Opens with "Hi [first name]," to your team.',
      'Opens with "Hello [first name]," to anyone else.',
      'Signs off "Cheers, Sam".',
      "Gets to the point in the first line.",
      "Never uses exclamation marks.",
    ],
  );
});

test("one greeting for everyone is one sentence", () => {
  const g = "Hi {first},";
  assert.deepEqual(styleSentences({ ...empty, greetings: { team: g, contact: g, other: g } }), [
    'Opens with "Hi [first name],".',
  ]);
});

test("length or tone alone still reads", () => {
  assert.deepEqual(styleSentences({ ...empty, formality: "formal" }), ["Usually formal emails."]);
  assert.deepEqual(styleSentences(empty), []);
});

test("only what the new style adds is marked new", () => {
  const current = { ...empty, length: "short", formality: "casual", habits: ["Uses bullet points for steps"] };
  const next = { ...current, sign_off: "Cheers,\nSam" };
  assert.deepEqual(markNew(current, next), [
    { text: "Usually short, casual emails.", isNew: false },
    { text: 'Signs off "Cheers, Sam".', isNew: true },
    { text: "Uses bullet points for steps.", isNew: false },
  ]);
});

test("a tapped phrase becomes its own sentence, once", () => {
  assert.equal(addPhrase("", "No emojis"), "No emojis.");
  assert.equal(addPhrase("I keep it short", "No emojis"), "I keep it short. No emojis.");
  assert.equal(addPhrase("I keep it short.  ", "No emojis"), "I keep it short. No emojis.");
  assert.equal(addPhrase("No emojis.", "No emojis"), "No emojis.");
});
