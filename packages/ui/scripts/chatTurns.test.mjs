import assert from "node:assert/strict";
import test from "node:test";
import { confirmRedoText, dropLastTurn, lastTurn } from "../src/lib/chatTurns.ts";

const user = (content) => ({ role: "user", content });
const reply = (content, id, actions) => ({ role: "assistant", content, id, ...(actions ? { actions } : {}) });
const WEB = "3f2c9a1e-7d1b-4b8e-9a0c-1f2e3d4c5b6a";

test("a saved last reply can be redone in a web chat", () => {
  const msgs = [user("hi"), reply("hello", 1), user("plan my week"), reply("here", 2)];
  assert.deepEqual(lastTurn(msgs, WEB), {
    replyIndex: 3,
    userIndex: 2,
    replyId: 2,
    canRedo: true,
    didSomething: false,
  });
});

test("the last user message is found past several queued ones", () => {
  const msgs = [user("a"), user("b"), reply("ok", 7)];
  const t = lastTurn(msgs, WEB);
  assert.equal(t.userIndex, 1);
  assert.equal(t.canRedo, true);
});

test("no redo without a session, in a channel chat, or before the reply is saved", () => {
  const msgs = [user("hi"), reply("hello", 1)];
  assert.equal(lastTurn(msgs, null).canRedo, false);
  assert.equal(lastTurn(msgs, "slack:dm:U123").canRedo, false);
  assert.equal(lastTurn(msgs, "telegram:42").canRedo, false);
  const unsaved = lastTurn([user("hi"), { role: "assistant", content: "..." }], WEB);
  assert.equal(unsaved.replyIndex, null);
  assert.equal(unsaved.replyId, null);
  assert.equal(unsaved.canRedo, false);
});

test("a chat ending on the user's message has no reply to redo", () => {
  const t = lastTurn([reply("hello", 1), user("still there?")], WEB);
  assert.equal(t.replyIndex, null);
  assert.equal(t.userIndex, null);
  assert.equal(t.canRedo, false);
  assert.equal(lastTurn([], WEB).canRedo, false);
});

test("a reply with actions did something", () => {
  const msgs = [user("email Sam"), reply("done", 4, [{ tool: "send_email", summary: "Sent an email to Sam" }])];
  assert.equal(lastTurn(msgs, WEB).didSomething, true);
  assert.equal(lastTurn([user("x"), reply("y", 5, [])], WEB).didSomething, false);
});

test("dropLastTurn removes the last reply and the messages it answered", () => {
  const msgs = [user("hi"), reply("hello", 1), user("a"), user("b"), reply("ok", 2)];
  assert.deepEqual(dropLastTurn(msgs), msgs.slice(0, 2));
  assert.deepEqual(dropLastTurn([user("only"), reply("one", 1)]), []);
  assert.deepEqual(dropLastTurn([]), []);
});

test("confirmRedoText names the first action, or says what kind", () => {
  assert.equal(
    confirmRedoText([{ summary: "Sent an email to Sam" }]),
    'That answer already did something ("Sent an email to Sam"). Doing it again could repeat it. Go ahead?',
  );
  assert.match(confirmRedoText([]), /sent a message, booked a meeting or filed a card/);
});
