import assert from "node:assert/strict";
import test from "node:test";
import {
  relationLabel,
  replyFlagLines,
  safeGmailLink,
  sendLeftNothing,
  sendQuestion,
  senderLine,
} from "../src/lib/replyCards.ts";

test("relationLabel names who the sender is, and nothing for an unknown relation", () => {
  assert.equal(relationLabel("team"), "Your team");
  assert.equal(relationLabel("stranger"), "New to you");
  assert.equal(relationLabel("correspondent"), "You've written to them before");
  assert.equal(relationLabel("boss"), "");
});

test("replyFlagLines puts the pressing warnings first, once each, and drops unknown flags", () => {
  const lines = replyFlagLines(["others_on_thread", "sender_unverified", "made_up", "others_on_thread"]);
  assert.equal(lines.length, 2);
  assert.match(lines[0], /couldn't confirm/);
  assert.match(lines[1], /sender only/);
  // The open questions already carry this one.
  assert.deepEqual(replyFlagLines(["asks_if_ai"]), []);
  assert.deepEqual(replyFlagLines([]), []);
});

test("senderLine shows the name and address, or the address alone", () => {
  assert.equal(senderLine({ from_name: "Dana Park", from_email: "dana@x.example" }), "Dana Park <dana@x.example>");
  assert.equal(senderLine({ from_name: "  ", from_email: "dana@x.example" }), "dana@x.example");
});

test("safeGmailLink keeps only a link into Gmail", () => {
  const link = "https://mail.google.com/mail/u/?authuser=o%40x.example#all/18c2";
  assert.equal(safeGmailLink(link), link);
  assert.equal(safeGmailLink("https://mail.google.com.evil.example/x"), "");
  assert.equal(safeGmailLink("javascript:alert(1)"), "");
  assert.equal(safeGmailLink(""), "");
});

test("sendQuestion names who the reply goes to", () => {
  assert.equal(
    sendQuestion(["dana@x.example", "sam@x.example"]),
    "Send this reply to dana@x.example, sam@x.example from your Gmail, exactly as the draft is there?",
  );
  assert.match(sendQuestion([]), /to the sender/);
});

test("a failed send says whether the card is gone", () => {
  assert.equal(sendLeftNothing("draft_gone"), true);
  assert.equal(sendLeftNothing("already_handled"), true);
  assert.equal(sendLeftNothing("send_unconfirmed"), false);
  assert.equal(sendLeftNothing("caller_signing_required"), false);
  // A failed send is the first warning on the card.
  assert.match(replyFlagLines(["others_on_thread", "send_failed"])[0], /didn't go through/);
});
