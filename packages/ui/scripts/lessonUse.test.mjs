import assert from "node:assert/strict";
import test from "node:test";
import { lessonUseLine, totalUses, weekUseLine } from "../src/lib/lessonUse.ts";

test("a lesson says where it was used, most used first", () => {
  assert.equal(
    lessonUseLine({ chat: 3, email: 2, one_on_one: 1 }),
    "used 6 times: 3 in chat, 2 in email, 1 in a 1:1",
  );
  assert.equal(lessonUseLine({ email: 1, meeting: 4 }), "used 5 times: 4 in meetings, 1 in email");
});

test("one surface reads as one", () => {
  assert.equal(lessonUseLine({ email: 4, chat: 0 }), "used 4 times, all in email");
  assert.equal(lessonUseLine({ brief: 1 }), "used once, in a brief");
});

test("a lesson nothing has used says so", () => {
  assert.equal(lessonUseLine({}), "not used yet");
  assert.equal(lessonUseLine(undefined), "not used yet");
  assert.equal(totalUses(undefined), 0);
});

test("the week's line names only the surfaces used", () => {
  assert.equal(weekUseLine({ chat: 20, one_on_one: 12, email: 9 }), "in chat, in your 1:1s and in email");
  assert.equal(weekUseLine({ email: 2 }), "in email");
  assert.equal(weekUseLine({}), "");
  assert.equal(totalUses({ chat: 20, one_on_one: 12, email: 9 }), 41);
});
