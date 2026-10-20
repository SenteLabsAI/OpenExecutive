import assert from "node:assert/strict";
import test from "node:test";
import {
  cardGist,
  toDecideLabel,
  highlightPieces,
  questionsLeft,
  isMailboxLink,
  mailboxName,
  relationLabel,
  replyFlagLines,
  replyToChoice,
  safeGmailLink,
  sendLeftNothing,
  sendQuestion,
  senderLine,
  senderShort,
  draftIsLong,
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
  assert.match(lines[1], /Others were on this email/);
  // The open questions already carry this one.
  assert.deepEqual(replyFlagLines(["asks_if_ai"]), []);
  assert.deepEqual(replyFlagLines([]), []);
});

test("senderLine shows the name and address, or the address alone", () => {
  assert.equal(senderLine({ from_name: "Dana Park", from_email: "dana@x.example" }), "Dana Park <dana@x.example>");
  assert.equal(senderLine({ from_name: "  ", from_email: "dana@x.example" }), "dana@x.example");
});

test("senderShort keeps the domain beside the name, and the full address unless confirmed", () => {
  const card = { from_name: "Dana Park", from_email: "dana@d4na-park.example" };
  assert.equal(senderShort({ ...card, sender_verified: true }), "Dana Park · @d4na-park.example");
  assert.equal(senderShort({ ...card, sender_verified: false }), "Dana Park <dana@d4na-park.example>");
  assert.equal(senderShort(card), "Dana Park <dana@d4na-park.example>");
  assert.equal(senderShort({ from_name: " ", from_email: "dana@x.example", sender_verified: true }), "dana@x.example");
});

test("draftIsLong clamps drafts past 280 characters or 5 lines", () => {
  assert.equal(draftIsLong("Thanks, Thursday works."), false);
  assert.equal(draftIsLong("x".repeat(281)), true);
  assert.equal(draftIsLong("a\nb\nc\nd\ne\nf"), true);
});

test("safeGmailLink keeps only a link into Gmail", () => {
  const link = "https://mail.google.com/mail/u/?authuser=o%40x.example#all/18c2";
  assert.equal(safeGmailLink(link), link);
  assert.equal(safeGmailLink("https://mail.google.com.evil.example/x"), "");
  assert.equal(safeGmailLink("javascript:alert(1)"), "");
  assert.equal(safeGmailLink(""), "");
});

test("Outlook on the web links are kept too, and name the mailbox", () => {
  for (const link of [
    "https://outlook.office.com/mail/deeplink/read/AAk%2Fd1",
    "https://outlook.live.com/mail/0/deeplink/read/AAk1",
  ]) {
    assert.equal(safeGmailLink(link), link);
    assert.equal(mailboxName(link), "Outlook");
  }
  assert.equal(safeGmailLink("https://outlook.office.com.evil.example/mail/x"), "");
  assert.equal(safeGmailLink("https://outlook.office.com/owa/x"), "");
  assert.equal(isMailboxLink("https://outlook.live.com/mail/0/drafts"), true);
  assert.equal(mailboxName("https://mail.google.com/mail/u/#drafts"), "Gmail");
  assert.match(sendQuestion(["d@x.example"], "Outlook"), /from your Outlook,/);
});

test("sendQuestion names who the reply goes to", () => {
  assert.equal(
    sendQuestion(["dana@x.example", "sam@x.example"]),
    "Send this reply to dana@x.example, sam@x.example from your Gmail, exactly as the draft is there?",
  );
  assert.match(sendQuestion([]), /to the sender/);
  // Kept on its card alone: it goes as the card shows it.
  assert.equal(
    sendQuestion(["dana@x.example"], "Outlook", false),
    "Send this reply to dana@x.example from your Outlook, as shown above?",
  );
});

test("a failed send says whether the card is gone", () => {
  assert.equal(sendLeftNothing("draft_gone"), true);
  assert.equal(sendLeftNothing("already_handled"), true);
  assert.equal(sendLeftNothing("send_unconfirmed"), false);
  assert.equal(sendLeftNothing("caller_signing_required"), false);
  // A failed send is the first warning on the card.
  assert.match(replyFlagLines(["others_on_thread", "send_failed"])[0], /didn't go through/);
});

test("questionsLeft counts only open questions with answers to tap, and none once edited by hand", () => {
  const questions = [
    { text: "When?", choices: ["Mon 10:00", "Tue 09:00"], answer: "" },
    { text: "Which?", choices: ["A", "B"], answer: "A" },
    { text: "Are you an AI?", choices: [], answer: "" },
  ];
  assert.equal(questionsLeft({ questions }), 1);
  assert.equal(questionsLeft({ questions, questions_settled: true }), 0);
  // An older backend or card: nothing holds Send back.
  assert.equal(questionsLeft({}), 0);
});

test("highlightPieces marks the new sentences and keeps every character in order", () => {
  const body = "Hi Dana,\n\nThanks. Friday at 10 works for me. See you then.";
  const pieces = highlightPieces(body, ["Friday at 10 works for me.", "Not in the body."]);
  assert.equal(pieces.map((p) => p.text).join(""), body);
  assert.deepEqual(pieces.filter((p) => p.changed).map((p) => p.text), ["Friday at 10 works for me."]);
  assert.deepEqual(highlightPieces(body, []), [{ text: body, changed: false }]);
});

test("replyToChoice offers the sender alone or everyone, and nothing without others", () => {
  const card = { from_name: "Dana Park", from_email: "dana@x.example", draft_to: ["dana@x.example"] };
  assert.equal(replyToChoice(card), null);
  assert.equal(replyToChoice({ ...card, reply_others: ["sam@x.example"], source: "follow_up" }), null);
  const choice = replyToChoice({ ...card, reply_others: ["sam@x.example", "lee@x.example"] });
  assert.deepEqual(choice, { sender: "Dana only", everyone: "Everyone (3)", toEveryone: false });
  const all = replyToChoice({
    ...card, from_name: "", reply_others: ["sam@x.example"], draft_to: ["dana@x.example", "sam@x.example"],
  });
  assert.equal(all.sender, "dana@x.example only");
  assert.equal(all.toEveryone, true);
});

test("cardGist shows the summary, or the start of what they wrote on an older card", () => {
  assert.equal(cardGist({ gist: " Dana asks when to talk. ", they_wrote: "Hi" }), "Dana asks when to talk.");
  assert.equal(cardGist({ they_wrote: "Hi Olivia,\n\n  Can we talk Thursday?" }), "Hi Olivia, Can we talk Thursday?");
  const long = cardGist({ gist: "", they_wrote: "word ".repeat(60) });
  assert.ok(long.endsWith("word…") && long.length <= 161);
  assert.equal(cardGist({ they_wrote: "" }), "");
});

test("toDecideLabel counts what's left, and is empty when nothing is", () => {
  assert.equal(toDecideLabel(1), "1 to decide");
  assert.equal(toDecideLabel(3), "3 to decide");
  assert.equal(toDecideLabel(0), "");
});
