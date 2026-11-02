import assert from "node:assert/strict";
import test from "node:test";
import {
  PIN_TOP_GAP,
  isNearBottom,
  lastUserIndex,
  pinScrollTop,
  pinSpacerHeight,
} from "../src/lib/chatPin.ts";

test("spacer leaves room for the reply under a just-sent message", () => {
  // View 800 tall, message at 1000, content ends at 1060 (just the bubble).
  assert.equal(pinSpacerHeight(800, 1000, 1060), 800 - PIN_TOP_GAP - 60);
});

test("spacer shrinks as the reply grows and stops at zero", () => {
  assert.equal(pinSpacerHeight(800, 1000, 1500), 800 - PIN_TOP_GAP - 500);
  assert.equal(pinSpacerHeight(800, 1000, 1000 + 800), 0);
  assert.equal(pinSpacerHeight(800, 1000, 5000), 0);
});

test("scroll target puts the message just under the top of the view", () => {
  // Message is 640px below the view's top; already scrolled 300.
  assert.equal(pinScrollTop(300, 700, 60), 300 + 640 - PIN_TOP_GAP);
  // Never negative.
  assert.equal(pinScrollTop(0, 70, 60), 0);
});

test("near the bottom counts as at the bottom", () => {
  assert.equal(isNearBottom(1200, 800, 2000), true);
  assert.equal(isNearBottom(1150, 800, 2000), true);
  assert.equal(isNearBottom(500, 800, 2000), false);
});

test("finds the newest message you sent", () => {
  assert.equal(lastUserIndex([]), null);
  assert.equal(lastUserIndex([{ role: "assistant" }]), null);
  assert.equal(
    lastUserIndex([{ role: "user" }, { role: "assistant" }, { role: "user" }, { role: "assistant" }]),
    2,
  );
});

test("a custom gap moves the pinned message and its room together", () => {
  assert.equal(pinSpacerHeight(800, 1000, 1060, 40), 800 - 40 - 60);
  assert.equal(pinScrollTop(300, 700, 60, 40), 300 + 640 - 40);
});
