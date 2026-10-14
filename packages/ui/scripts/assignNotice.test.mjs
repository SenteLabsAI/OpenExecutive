import assert from "node:assert/strict";
import test from "node:test";
import { assignNoticeLine } from "../src/lib/assignNotice.ts";

const FOLLOW = "They'll be followed up if it isn't done by then.";

test("a sent notice names the channel", () => {
  assert.equal(
    assignNoticeLine({ status: "sent", channel: "slack_dm" }),
    `They were told on Slack. ${FOLLOW}`,
  );
  assert.equal(assignNoticeLine({ status: "sent", channel: "weird" }), `They were told. ${FOLLOW}`);
});

test("held, unreached and skipped notices read plainly", () => {
  assert.match(assignNoticeLine({ status: "held", channel: null }), /quiet hours end/);
  assert.match(assignNoticeLine({ status: "not_reached", channel: null }), /couldn't be reached/);
  assert.equal(assignNoticeLine({ status: "skipped", channel: null }), FOLLOW);
  assert.equal(assignNoticeLine(undefined), FOLLOW);
});
