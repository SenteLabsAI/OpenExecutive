import assert from "node:assert/strict";
import test from "node:test";
import { HANDLE_IT_MODES } from "../src/lib/reach.ts";
import {
  LEAD_OPTIONS,
  SCENES,
  SEND_OPTIONS,
  TOUR,
  WRITE_OPTIONS,
  nextInTour,
  sceneFor,
  sceneKey,
} from "../src/lib/whoSends.ts";

test("every option has its own scene", () => {
  const keys = [
    ...WRITE_OPTIONS.map((o) => sceneKey("writes", o.value)),
    ...SEND_OPTIONS.map((o) => sceneKey("sends", o.value)),
    ...LEAD_OPTIONS.map((o) => sceneKey("lead", o.value)),
  ];
  assert.deepEqual([...keys].sort(), SCENES.map((s) => s.key).sort());
  for (const s of SCENES) assert.ok(s.frames.length >= 2, `${s.key} has frames`);
});

test("the sending levels use the names Handle it for me shows", () => {
  const levels = SEND_OPTIONS.filter((o) => o.value !== "off" && o.value !== "everything");
  assert.deepEqual(levels, HANDLE_IT_MODES.map((m) => ({ value: m.mode, label: m.label })));
});

test("a scene lights the option it plays", () => {
  for (const s of SCENES) {
    const [group, value] = s.key.split(":");
    const lit = { writes: s.writes, sends: s.sends, lead: s.lead }[group];
    assert.equal(lit, value, s.key);
  }
});

test("anything it sends on its own rides on For my inbox too", () => {
  for (const s of SCENES.filter((x) => x.sends !== "off")) assert.equal(s.writes, "inbox", s.key);
});

test("the tour plays one scene per tab and loops", () => {
  assert.deepEqual(TOUR.map((k) => sceneFor(k).tab), ["act", "handle", "lead"]);
  assert.equal(nextInTour(TOUR[2]), TOUR[0]);
  assert.equal(sceneFor("nope").key, SCENES[0].key);
});
