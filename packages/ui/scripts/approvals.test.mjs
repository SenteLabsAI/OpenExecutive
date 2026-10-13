import assert from "node:assert/strict";
import test from "node:test";
import { approvalLine, chatHref } from "../src/lib/approvals.ts";

const base = {
  id: 1,
  alert_id: 7,
  approved_by_person_id: 2,
  approved_at: "2026-10-13T20:00:00Z",
  edited_body: null,
  outcome: "",
  session_id: null,
  finished_at: null,
};

test("a running approval links to the chat doing the work", () => {
  const line = approvalLine({ ...base, status: "running", session_id: "abc 1" }, "Brief the board");
  assert.equal(line.text, "Working on it: Brief the board");
  assert.equal(line.chatHref, "/?session=abc%201");
  assert.equal(line.chatLabel, "Watch it");
  assert.equal(line.canRetry, false);
});

test("a done approval without a chat says what came of it", () => {
  const line = approvalLine(
    { ...base, status: "done", outcome: "Queued to go out now." },
    "Send the recap",
  );
  assert.equal(line.text, "Done: Send the recap. Queued to go out now.");
  assert.equal(line.chatHref, null);
  assert.equal(line.tone, "emerald");
});

test("a failed approval can be tried again", () => {
  const line = approvalLine({ ...base, status: "failed", outcome: "It couldn't be started" }, "Ping Ana");
  assert.equal(line.text, "Didn't work: Ping Ana. It couldn't be started");
  assert.equal(line.canRetry, true);
  assert.equal(line.tone, "rose");
});

test("no chat, no link", () => {
  assert.equal(chatHref(null), null);
});
