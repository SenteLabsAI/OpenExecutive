import assert from "node:assert/strict";
import test from "node:test";
import { clock, joinDictation, pickRecordingType, recordingFilename } from "../src/lib/dictation.ts";

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
