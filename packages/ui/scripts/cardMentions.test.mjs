// A card number in a web chat reply links to the card on Home
// (lib/cardMentions.ts), and Home opens on the card a link asks for.
import assert from "node:assert/strict";
import test from "node:test";
import { cardFromSearch, linkCardMentions } from "../src/lib/cardMentions.ts";

test("a card number becomes a link to that card on Home", () => {
  assert.equal(linkCardMentions("Review card 220 today."), "Review [that card](/?card=220) today.");
  assert.equal(linkCardMentions("Card #31 is waiting."), "[That card](/?card=31) is waiting.");
  assert.equal(linkCardMentions("Approve the card 7 first."), "Approve [that card](/?card=7) first.");
  assert.equal(linkCardMentions("alert 12 cleared"), "[that card](/?card=12) cleared");
});

test("links, paths and other words are left as written", () => {
  for (const text of [
    "See /cards/220 for it.",
    "[card 5](/?card=5)",
    "Your scorecard 5 looks fine.",
    "Paid with the card ending 4242.",
    "Set an alert 30 minutes before.",
    "That is card 2 of 5.",
    "Run `alert 500` here.",
    "```\ncard 220\n```",
    "",
  ]) {
    assert.equal(linkCardMentions(text), text);
  }
});

test("Home reads the card a link asks for", () => {
  assert.equal(cardFromSearch("?card=220"), 220);
  assert.equal(cardFromSearch("?card=abc"), null);
  assert.equal(cardFromSearch("?session=x"), null);
});
