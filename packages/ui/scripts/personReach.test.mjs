import assert from "node:assert/strict";
import test from "node:test";
import { availableChannels, onLeaveLine, reachLine } from "../src/lib/personReach.ts";

const base = {
  email: null,
  slack_user_id: null,
  discord_user_id: null,
  telegram_chat_id: null,
  preferred_channel: "any",
  response_sla_hours: 24,
};

test("a preferred channel is named with the reply time", () => {
  const p = { ...base, preferred_channel: "discord", discord_user_id: "100000000000000001", response_sla_hours: 12 };
  assert.equal(reachLine(p), "Reaches them on Discord, replies within 12h");
});

test("any channel lists what they have, email first", () => {
  const p = { ...base, email: "ada@example.com", telegram_chat_id: "123" };
  assert.deepEqual(availableChannels(p), ["email", "telegram"]);
  assert.equal(reachLine(p, { self: true }), "Reaches you by email or Telegram, replies within 24h");
  const three = { ...p, slack_user_id: "U01" };
  assert.equal(reachLine(three), "Reaches them by email, Slack or Telegram, replies within 24h");
});

test("a contact has no reply time", () => {
  const p = { ...base, email: "ada@example.com", preferred_channel: "email" };
  assert.equal(reachLine(p, { contact: true }), "Reaches them by email");
});

test("nothing set says so", () => {
  assert.equal(reachLine(base), "No email or chat app set for them yet");
});

test("on leave shows a future date and drops a past one", () => {
  const today = new Date(2026, 9, 15);
  assert.equal(onLeaveLine("2026-10-24", today), "On leave until 24 Oct");
  assert.equal(onLeaveLine("2027-01-03", today), "On leave until 3 Jan 2027");
  assert.equal(onLeaveLine("2026-10-01", today), null);
  assert.equal(onLeaveLine(null, today), null);
});
