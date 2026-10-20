import assert from "node:assert/strict";
import test from "node:test";
import { cardEntry, noteAboutYou } from "../src/lib/aboutYou.ts";

test("a tagged pair becomes a labelled row", () => {
  assert.deepEqual(cardEntry("ATTRIBUTE: Email: ada@example.com"), {
    label: "Email",
    value: "ada@example.com",
  });
  assert.deepEqual(cardEntry("ATTRIBUTE: Phone: 555-0100"), { label: "Phone", value: "555-0100" });
});

test("a tagged sentence loses only its tag", () => {
  assert.deepEqual(cardEntry("ATTRIBUTE: Works in Operations at Northwind Supply"), {
    label: null,
    value: "Works in Operations at Northwind Supply",
  });
  assert.deepEqual(cardEntry("RELATIONSHIP: Reports to the CFO"), { label: null, value: "Reports to the CFO" });
});

test("untagged lines and links stay as written", () => {
  assert.deepEqual(cardEntry("Prefers short answers"), { label: null, value: "Prefers short answers" });
  assert.deepEqual(cardEntry("https://example.com/a"), { label: null, value: "https://example.com/a" });
  // A long lead-in before a colon is a sentence, not a label.
  const long = "Said in the Monday meeting that the plan was: ship it";
  assert.deepEqual(cardEntry(long), { label: null, value: long });
});

test("notes name the person instead of their peer id", () => {
  assert.equal(
    noteAboutYou("On 2026-10-01, peer 3 referenced a 'Harbor Point' document", 3, "Ada Lovelace"),
    "Ada referenced a 'Harbor Point' document",
  );
  assert.equal(noteAboutYou("3 asked what the discussion is about", 3, "Ada Lovelace"), "Ada asked what the discussion is about");
  assert.equal(noteAboutYou("3's team owns the budget", 3, "Ada Lovelace"), "Ada's team owns the budget");
  assert.equal(noteAboutYou("Prefers that peer_3 sees numbers first", 3, "Ada"), "Prefers that Ada sees numbers first");
});

test("other numbers and other people are left alone", () => {
  assert.equal(noteAboutYou("30 brokers were asked about tiers", 3, "Ada"), "30 brokers were asked about tiers");
  assert.equal(noteAboutYou("peer 31 asked about a proposal", 3, "Ada"), "Peer 31 asked about a proposal");
  assert.equal(noteAboutYou("Asked for 3 quotes", 3, "Ada"), "Asked for 3 quotes");
});
