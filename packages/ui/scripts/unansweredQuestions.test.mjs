import assert from "node:assert/strict";
import test from "node:test";
import { askedLine } from "../src/lib/unansweredQuestions.ts";

test("names who asked, where and when", () => {
  assert.equal(
    askedLine({ asked_by: "Sam Okafor", place: "chat", times: 1 }, "2d ago"),
    "Sam Okafor asked in chat · 2d ago"
  );
});

test("adds the count only when it was asked more than once", () => {
  assert.equal(
    askedLine({ asked_by: "You", place: "Slack", times: 3 }, "3h ago"),
    "You asked in Slack · 3h ago · asked 3 times"
  );
});

test("leaves out a time it can't read", () => {
  assert.equal(askedLine({ asked_by: "You", place: "chat", times: 1 }, ""), "You asked in chat");
});
