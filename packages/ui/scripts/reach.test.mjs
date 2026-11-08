import assert from "node:assert/strict";
import test from "node:test";
import { APPROVALS_SENDS, SENDS_HREF, WRITES_HREF, reachForSend, reachOf, sendOf, sentTodayLine, writeOf } from "../src/lib/reach.ts";

const settings = (over = {}) => ({
  enabled: true,
  inbox: { enabled: true },
  handle_it: { enabled: true, available: true, sent_today: 0 },
  training: { drafts: false, learned: [] },
  ...over,
});

test("reachOf reads the dial step from the settings", () => {
  assert.equal(reachOf(settings({ enabled: false })), "off");
  assert.equal(reachOf(settings({ inbox: { enabled: false } })), "when_asked");
  assert.equal(reachOf(settings({ handle_it: { enabled: false } })), "inbox");
  assert.equal(reachOf(settings()), "handle_it");
});

test("writeOf puts Handle it for me at Draft replies automatically", () => {
  assert.equal(writeOf(settings({ enabled: false })), "off");
  assert.equal(writeOf(settings({ inbox: { enabled: false } })), "when_asked");
  assert.equal(writeOf(settings({ handle_it: { enabled: false } })), "inbox");
  assert.equal(writeOf(settings()), "inbox");
});

test("sendOf is on only with Handle it for me on", () => {
  assert.equal(sendOf(settings({ enabled: false })), "off");
  assert.equal(sendOf(settings({ inbox: { enabled: false } })), "off");
  assert.equal(sendOf(settings({ handle_it: { enabled: false } })), "off");
  assert.equal(sendOf(settings()), "on");
});

test("turning Handle it for me off keeps the inbox replies", () => {
  assert.equal(reachForSend("off"), "inbox");
  assert.equal(reachForSend("on"), "handle_it");
});

test("each Approvals level says what Handle it for me sends", () => {
  assert.deepEqual(Object.keys(APPROVALS_SENDS), ["ask", "outsiders", "trust"]);
  assert.match(APPROVALS_SENDS.ask.line, /Nothing goes on its own/);
});

test("each picker has its own link", () => {
  assert.equal(WRITES_HREF, "/delegate/act-as-me#writes");
  assert.equal(SENDS_HREF, "/delegate/handle-it#sends");
  assert.equal(sentTodayLine(1), "Sent 1 reply on its own today.");
  assert.equal(sentTodayLine(3), "Sent 3 replies on its own today.");
});
