import assert from "node:assert/strict";
import test from "node:test";
import { upstreamSegment } from "../src/lib/proxySegments.ts";

test("ordinary segments go on unchanged", () => {
  assert.equal(upstreamSegment("sessions"), "sessions");
  assert.equal(upstreamSegment("telegram:42"), "telegram:42");
});

test("an id holding a slash goes on as one base64url segment", () => {
  const id = "google_chat:spaces/AAQA:spaces/AAQA/threads/x1";
  const out = upstreamSegment(id);
  assert.ok(out.startsWith("~"));
  assert.match(out.slice(1), /^[A-Za-z0-9_-]+$/);
  assert.equal(Buffer.from(out.slice(1), "base64url").toString("utf8"), id);
});

test("a slashed segment can't walk out of the path", () => {
  const out = upstreamSegment("../../auth/accounts");
  assert.ok(!out.includes("/") && !out.includes("."));
});
