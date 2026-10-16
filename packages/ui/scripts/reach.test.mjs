import assert from "node:assert/strict";
import test from "node:test";
import { SENDS_HREF, WRITES_HREF, reachForSend, reachOf, sendOf, sentTodayLine, writeOf } from "../src/lib/reach.ts";

const settings = (over = {}) => ({
  enabled: true,
  inbox: { enabled: true },
  handle_it: { enabled: true, mode: "balanced", available: true, sent_today: 0, lead: false },
  training: { replies: false, follow_ups: false, drafts: false },
  ...over,
});

test("reachOf reads the dial step from the settings", () => {
  assert.equal(reachOf(settings({ enabled: false })), "off");
  assert.equal(reachOf(settings({ inbox: { enabled: false } })), "when_asked");
  assert.equal(reachOf(settings({ handle_it: { enabled: false, mode: "balanced" } })), "inbox");
  assert.equal(reachOf(settings()), "balanced");
  assert.equal(reachOf(settings({ handle_it: { enabled: true, mode: "bold", lead: true } })), "everything");
});

test("writeOf puts every sending step at Draft replies automatically", () => {
  assert.equal(writeOf(settings({ enabled: false })), "off");
  assert.equal(writeOf(settings({ inbox: { enabled: false } })), "when_asked");
  assert.equal(writeOf(settings({ handle_it: { enabled: false, mode: "balanced" } })), "inbox");
  assert.equal(writeOf(settings()), "inbox");
  assert.equal(writeOf(settings({ handle_it: { enabled: true, mode: "bold", lead: true } })), "inbox");
});

test("sendOf is off below the sending steps", () => {
  assert.equal(sendOf(settings({ enabled: false })), "off");
  assert.equal(sendOf(settings({ inbox: { enabled: false } })), "off");
  assert.equal(sendOf(settings({ handle_it: { enabled: false, mode: "balanced" } })), "off");
  assert.equal(sendOf(settings()), "balanced");
  assert.equal(sendOf(settings({ handle_it: { enabled: true, mode: "bold", lead: true } })), "everything");
});

test("turning Handle it for me off keeps the inbox replies", () => {
  assert.equal(reachForSend("off"), "inbox");
  assert.equal(reachForSend("careful"), "careful");
  assert.equal(reachForSend("everything"), "everything");
});

test("each picker has its own link", () => {
  assert.equal(WRITES_HREF, "/delegate/act-as-me#writes");
  assert.equal(SENDS_HREF, "/delegate/handle-it#sends");
  assert.equal(sentTodayLine(1), "Sent 1 reply on its own today.");
  assert.equal(sentTodayLine(3), "Sent 3 replies on its own today.");
});
