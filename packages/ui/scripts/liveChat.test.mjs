import assert from "node:assert/strict";
import test from "node:test";
import { applyLiveEvent, EMPTY_LIVE, peopleLine, TYPING_SHOWS_MS, typingLine } from "../src/lib/liveChat.ts";

const opts = (over = {}) => ({ mine: () => false, you: 1, now: 1000, ...over });
const started = {
  type: "turn_started",
  turn_key: "t1",
  sender_person_id: 2,
  sender_name: "Riya Patel",
  content: "Can the status column be green?",
};

test("someone else's turn shows as it runs and is read back when it ends", () => {
  let step = applyLiveEvent(EMPTY_LIVE, started, opts());
  assert.equal(step.state.turn?.senderName, "Riya Patel");
  assert.equal(step.state.turn?.thinking, true);

  step = applyLiveEvent(step.state, { type: "activity", turn_key: "t1", label: "Checking the app" }, opts());
  assert.equal(step.state.turn?.activity, "Checking the app");

  step = applyLiveEvent(step.state, { type: "chunk", turn_key: "t1", content: "Done" }, opts());
  step = applyLiveEvent(step.state, { type: "chunk", turn_key: "t1", content: "." }, opts());
  assert.equal(step.state.turn?.reply, "Done.");
  assert.equal(step.state.turn?.thinking, false);
  assert.equal(step.state.turn?.activity, null);

  step = applyLiveEvent(step.state, { type: "turn_ended", turn_key: "t1" }, opts());
  assert.equal(step.state.turn, null);
  assert.equal(step.reload, true);
});

test("this page's own turn is skipped: its own stream shows it", () => {
  const mine = opts({ mine: (key) => key === "t1" });
  let step = applyLiveEvent(EMPTY_LIVE, started, mine);
  assert.equal(step.state.turn, null);
  step = applyLiveEvent(step.state, { type: "chunk", turn_key: "t1", content: "x" }, mine);
  assert.equal(step.state.turn, null);
  step = applyLiveEvent(step.state, { type: "turn_ended", turn_key: "t1" }, mine);
  assert.equal(step.reload, false);
});

test("events for another turn change nothing", () => {
  const { state } = applyLiveEvent(EMPTY_LIVE, started, opts());
  const step = applyLiveEvent(state, { type: "chunk", turn_key: "other", content: "x" }, opts());
  assert.equal(step.state.turn?.reply, "");
});

test("opening mid-turn picks up the reply so far and reads the chat", () => {
  const step = applyLiveEvent(
    EMPTY_LIVE,
    {
      type: "ready",
      turn: { turn_key: "t1", sender_person_id: 2, sender_name: "Riya", content: "hi", reply: "Hel", actions: [] },
    },
    opts(),
  );
  assert.equal(step.reload, true);
  assert.equal(step.state.turn?.reply, "Hel");
  assert.equal(step.state.turn?.thinking, false);
});

test("typing shows for a while, never for the viewer, and stops when they send", () => {
  let step = applyLiveEvent(EMPTY_LIVE, { type: "typing", person_id: 1, name: "Me" }, opts());
  assert.equal(step.state.typing.length, 0);

  step = applyLiveEvent(EMPTY_LIVE, { type: "typing", person_id: 2, name: "Riya Patel" }, opts());
  assert.equal(typingLine(step.state.typing, 1000), "Riya is typing…");
  assert.equal(typingLine(step.state.typing, 1000 + TYPING_SHOWS_MS + 1), null);

  step = applyLiveEvent(step.state, started, opts());
  assert.equal(step.state.typing.length, 0);
});

test("typing line names one or two people, then sums up", () => {
  const t = (personId, name) => ({ personId, name, until: 5000 });
  assert.equal(typingLine([t(2, "Riya Patel"), t(3, "Alex Kim")], 0), "Riya and Alex are typing…");
  assert.equal(typingLine([t(2, "A"), t(3, "B"), t(4, "C")], 0), "Several people are typing…");
});

test("joins, leaves and endings", () => {
  assert.equal(applyLiveEvent(EMPTY_LIVE, { type: "people" }, opts()).people, true);
  assert.equal(applyLiveEvent(EMPTY_LIVE, { type: "resync" }, opts()).reload, true);
  const { state } = applyLiveEvent(EMPTY_LIVE, started, opts());
  const removed = applyLiveEvent(state, { type: "removed" }, opts());
  assert.equal(removed.closed, "removed");
  assert.equal(removed.state.turn, null);
  assert.equal(applyLiveEvent(state, { type: "deleted" }, opts()).closed, "deleted");
});

test("people line", () => {
  assert.equal(peopleLine([]), "Only you");
  assert.equal(peopleLine(["Riya Patel"]), "You and Riya");
  assert.equal(peopleLine(["Riya Patel", "Alex Kim"]), "You, Riya and Alex");
  assert.equal(peopleLine(["A", "B", "C"]), "You and 3 others");
});
