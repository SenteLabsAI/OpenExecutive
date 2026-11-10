import assert from "node:assert/strict";
import test from "node:test";
import { MIC_TONES, clock, joinDictation, pickRecordingType, recordingFilename, splitHeard } from "../src/lib/dictation.ts";

test("what was heard follows what the box held", () => {
  assert.equal(joinDictation("", "  move my call  "), "move my call");
  assert.equal(joinDictation("Hi.", "move my call"), "Hi. move my call");
  assert.equal(joinDictation("Hi.\n", "move my call"), "Hi.\nmove my call");
  assert.equal(joinDictation("Hi.", "   "), "Hi.");
});

test("the first recording type the browser takes is used", () => {
  assert.equal(pickRecordingType((t) => t === "audio/mp4"), "audio/mp4");
  assert.equal(pickRecordingType(() => true), "audio/webm;codecs=opus");
  assert.equal(pickRecordingType(() => false), undefined);
  assert.equal(pickRecordingType(() => { throw new Error("no"); }), undefined);
});

test("the filename says the format", () => {
  assert.equal(recordingFilename("audio/webm;codecs=opus"), "recording.webm");
  assert.equal(recordingFilename("audio/mp4"), "recording.m4a");
  assert.equal(recordingFilename("audio/ogg;codecs=opus"), "recording.ogg");
  assert.equal(recordingFilename(""), "recording.webm");
});

test("the timer reads minutes and seconds", () => {
  assert.equal(clock(7.9), "0:07");
  assert.equal(clock(102), "1:42");
  assert.equal(clock(-3), "0:00");
});

test("live words: settled ones follow what came before, the latest guess stays apart", () => {
  const parts = [
    { transcript: "move the board prep", isFinal: true },
    { transcript: " to Thursday", isFinal: true },
    { transcript: " and ask", isFinal: false },
    { transcript: " Dan", isFinal: false },
  ];
  assert.deepEqual(splitHeard("", parts), { settled: "move the board prep to Thursday", pending: "and ask Dan" });
  assert.deepEqual(splitHeard("Earlier words", parts.slice(0, 1)), {
    settled: "Earlier words move the board prep",
    pending: "",
  });
  assert.deepEqual(splitHeard("", []), { settled: "", pending: "" });
});

test("live words from Chrome on Android, which repeats what came before, don't repeat", () => {
  const parts = [
    { transcript: "move the", isFinal: true },
    { transcript: "move the board prep", isFinal: true },
    { transcript: "move the board prep to Thursday", isFinal: false },
  ];
  assert.deepEqual(splitHeard("", parts, true), { settled: "", pending: "move the board prep to Thursday" });
  assert.deepEqual(splitHeard("", parts.slice(0, 2), true), { settled: "move the board prep", pending: "" });
  assert.deepEqual(splitHeard("", [{ transcript: "the", isFinal: true }, { transcript: "theory", isFinal: true }], true), {
    settled: "the theory",
    pending: "",
  });
  assert.deepEqual(splitHeard("", [{ transcript: "hello", isFinal: true }, { transcript: "Hello, how are you", isFinal: false }], true), {
    settled: "",
    pending: "Hello, how are you",
  });
  // Desktop sends only new words, so a repeated word is kept.
  const desktop = [
    { transcript: "very", isFinal: true },
    { transcript: " very good", isFinal: true },
  ];
  assert.deepEqual(splitHeard("", desktop), { settled: "very very good", pending: "" });
});

test("the start tone rises and the stop tone falls", () => {
  assert.ok(MIC_TONES.start[1] > MIC_TONES.start[0]);
  assert.ok(MIC_TONES.stop[1] < MIC_TONES.stop[0]);
});
