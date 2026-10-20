import assert from "node:assert/strict";
import test from "node:test";
import {
  countByChannel,
  filterSessions,
  recentWebChats,
  sessionChannel,
} from "../src/lib/sessionChannel.ts";

const s = (session_id, title = session_id) => ({
  session_id,
  title,
  created_at: "2026-09-20T10:00:00Z",
  updated_at: "2026-09-20T10:00:00Z",
  message_count: 1,
});

test("session ids are classified by their adapter prefix", () => {
  assert.equal(sessionChannel("3f1c2a9e-8b1d-4c3e-9f0a-1b2c3d4e5f60"), "web");
  assert.equal(sessionChannel("slack:dm:U123"), "slack");
  assert.equal(sessionChannel("slack:thread:C1:1726000000.0001"), "slack");
  assert.equal(sessionChannel("telegram:98765"), "telegram");
  assert.equal(sessionChannel("discord:thread:111:222"), "discord");
  assert.equal(sessionChannel("email:abc@example.com"), "email");
  assert.equal(sessionChannel("google_chat:spaces/X:t1"), "google_chat");
});

test("unknown prefixes and odd ids fall back to web", () => {
  assert.equal(sessionChannel("carrier-pigeon:1"), "web");
  assert.equal(sessionChannel(":leading-colon"), "web");
  assert.equal(sessionChannel(""), "web");
  // Prefix match is exact, not substring: "slackish" is not Slack.
  assert.equal(sessionChannel("slackish:1"), "web");
});

test("recentWebChats keeps order, skips channel sessions and stops at the limit", () => {
  const sessions = [
    s("a"),
    s("slack:dm:U1"),
    s("b"),
    s("telegram:1"),
    s("c"),
    s("d"),
  ];
  assert.deepEqual(
    recentWebChats(sessions, 3).map((x) => x.session_id),
    ["a", "b", "c"],
  );
  assert.deepEqual(recentWebChats(sessions, 0), []);
  assert.equal(recentWebChats([s("slack:dm:U1")], 5).length, 0);
});

test("filterSessions combines channel and case-insensitive title query", () => {
  const sessions = [
    s("a", "Board prep for Q4"),
    s("slack:dm:U1", "Slack DM — Priya"),
    s("b", ""),
    s("discord:dm:9", "Board sync"),
  ];
  const ids = (xs) => xs.map((x) => x.session_id);
  assert.deepEqual(ids(filterSessions(sessions, { channel: "all", query: "" })), [
    "a",
    "slack:dm:U1",
    "b",
    "discord:dm:9",
  ]);
  assert.deepEqual(ids(filterSessions(sessions, { channel: "all", query: "  BOARD " })), [
    "a",
    "discord:dm:9",
  ]);
  assert.deepEqual(ids(filterSessions(sessions, { channel: "web", query: "board" })), ["a"]);
  assert.deepEqual(ids(filterSessions(sessions, { channel: "slack", query: "" })), [
    "slack:dm:U1",
  ]);
  // An empty title is searchable by the label the UI shows for it.
  assert.deepEqual(ids(filterSessions(sessions, { channel: "all", query: "untitled" })), ["b"]);
});

test("countByChannel only lists channels that have sessions", () => {
  assert.deepEqual(
    countByChannel([s("a"), s("b"), s("slack:dm:U1"), s("telegram:1"), s("telegram:2")]),
    { web: 2, slack: 1, telegram: 2 },
  );
  assert.deepEqual(countByChannel([]), {});
});
