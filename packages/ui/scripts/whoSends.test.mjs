import assert from "node:assert/strict";
import test from "node:test";
import { HANDLE_IT_MODES } from "../src/lib/reach.ts";
import { FIRST, MAILS, OPTIONS, SCENES, SEND_OPTIONS, beats, sceneFor, sceneKey } from "../src/lib/whoSends.ts";

test("every option has its own scene, named as its button", () => {
  const keys = [];
  for (const [tab, options] of Object.entries(OPTIONS)) {
    for (const o of options) {
      const s = sceneFor(sceneKey(tab, o.value));
      assert.equal(s.key, sceneKey(tab, o.value));
      assert.equal(s.tab, tab);
      assert.equal(s.option, o.label);
      keys.push(s.key);
    }
  }
  assert.deepEqual(keys.sort(), SCENES.map((s) => s.key).sort());
});

test("the sending levels use the names Handle it for me shows", () => {
  const levels = SEND_OPTIONS.filter((o) => o.value !== "off" && o.value !== "everything");
  assert.deepEqual(levels, HANDLE_IT_MODES.map((m) => ({ value: m.mode, label: m.label })));
});

test("each sending level sends more on its own, and contracts always wait", () => {
  const counts = SEND_OPTIONS.map((o) => sceneFor(sceneKey("handle", o.value)).sends);
  assert.equal(counts[0], 0);
  for (let i = 1; i < counts.length; i++) assert.ok(counts[i] > counts[i - 1], `level ${i}`);
  assert.ok(counts.at(-1) < MAILS.length);
  assert.match(MAILS.at(-1).why, /always wait/);
});

test("story scenes have steps, and every scene plays at least two beats", () => {
  for (const s of SCENES) {
    assert.equal(Boolean(s.steps), s.tab !== "handle", s.key);
    assert.ok(beats(s) >= 2, s.key);
  }
});

test("each tab opens on one of its own options", () => {
  for (const [tab, value] of Object.entries(FIRST)) assert.equal(sceneFor(sceneKey(tab, value)).tab, tab);
  assert.equal(sceneFor("nope").key, "writes:when_asked");
});
