import assert from "node:assert/strict";
import test from "node:test";
import { MIC_TONES, clock, joinDictation, liveWordsAllowed, pickRecordingType, recordingFilename, splitHeard } from "../src/lib/dictation.ts";

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

test("a repeated word is kept", () => {
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

test("no live words on Android, where they would silence the recording", () => {
  assert.equal(liveWordsAllowed("Mozilla/5.0 (Linux; Android 14; SM-F966U) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"), false);
  // "Request desktop site" hides Android from the user agent but not the platform.
  assert.equal(liveWordsAllowed("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", "Android"), false);
  assert.equal(liveWordsAllowed("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", "macOS"), true);
  assert.equal(liveWordsAllowed("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"), true);
});
