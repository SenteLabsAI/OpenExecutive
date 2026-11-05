import assert from "node:assert/strict";
import { test } from "node:test";

import { anyWorking, doneLink, statusLabel } from "../src/lib/doneForYou.ts";

test("a row's chip says where it is", () => {
  assert.equal(statusLabel("working"), "Working on it");
  assert.equal(statusLabel("done"), "Done");
  assert.equal(statusLabel("failed"), "Didn't work");
});

test("a row opens the chat that did it, else its write-up", () => {
  assert.deepEqual(doneLink({ kind: "approval", status: "working", session_id: "s 1" }), {
    href: "/?session=s%201", label: "Watch",
  });
  assert.equal(doneLink({ kind: "approval", status: "done", session_id: "s1" })?.label, "See");
  assert.deepEqual(doneLink({ kind: "workflow_done", status: "done", artifact_id: "a1" }), {
    href: "/artifacts/a1", label: "Read it",
  });
  assert.equal(doneLink({ kind: "sent_as_you", status: "done" }), null);
});

test("Home checks again only while something is working", () => {
  assert.equal(anyWorking([{ kind: "a", status: "done" }, { kind: "b", status: "working" }]), true);
  assert.equal(anyWorking([{ kind: "a", status: "failed" }]), false);
  assert.equal(anyWorking([]), false);
});
