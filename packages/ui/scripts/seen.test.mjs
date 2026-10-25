// A Yes inside a frame counts only once the browser reported it seen and the
// person pointed at it (lib/seen.ts): FrameConfirm.tsx uses it, with the
// observer and events faked here.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DWELL_MS,
  OBSERVE_DELAY_MS,
  SEEN_MS,
  SETTLE_MS,
  SeenGate,
  tracksVisibility,
  watchPanel,
} from "../src/lib/seen.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

const seen = (time, extra = {}) => ({ isIntersecting: true, intersectionRatio: 1, isVisible: true, time, ...extra });
const real = (now, extra = {}) => ({ isTrusted: true, onYes: true, now, ...extra });
const T = SEEN_MS + DWELL_MS;

/** Seen from 0, the pointer in from 0 and moved: armed from max(SEEN_MS, DWELL_MS). */
function pointedGate() {
  const gate = new SeenGate();
  gate.observe(seen(0));
  gate.pointerInside(true, 0, false);
  gate.pointerInside(true, 10, true);
  return gate;
}

/** A press then a click on Yes at `at`: whether the click counts. */
function pressAndClick(gate, at, press = {}, click = {}) {
  gate.pressed(real(at, press));
  return gate.clickCounts(real(at + 80, click));
}

test("a Yes counts once seen long enough, pointed at for a moment, pressed and clicked for real", () => {
  assert.equal(pressAndClick(pointedGate(), T), true);
  assert.ok(SEEN_MS >= 700);
  assert.ok(DWELL_MS >= 500, "longer than a double-click");
});

test("a Yes is refused when the panel was seen for under SEEN_MS", () => {
  const gate = new SeenGate();
  gate.observe(seen(1000));
  gate.pointerInside(true, 0, false);
  gate.pointerInside(true, 1001, true);
  assert.equal(pressAndClick(gate, 1000 + SEEN_MS - 100), false);
});

test("a Yes is refused while the panel is covered, see-through, transformed, cropped or hidden", () => {
  // The browser reports all of these as not visible; off screen or cropped as not whole.
  for (const report of [
    { isVisible: false },
    { isIntersecting: false, intersectionRatio: 0 },
    { intersectionRatio: 0.5 },
    { isVisible: undefined },
  ]) {
    const gate = pointedGate();
    gate.observe(seen(100, report));
    assert.equal(pressAndClick(gate, 5_000), false, JSON.stringify(report));
  }
});

test("a break starts the run again, and the pointer must move again after it", () => {
  const gate = pointedGate();
  gate.observe(seen(400, { isVisible: false }));
  gate.observe(seen(500));
  assert.equal(pressAndClick(gate, 500 + T), false, "no move since the break");
  gate.pointerInside(true, 600, true);
  assert.equal(pressAndClick(gate, 500 + SEEN_MS - 1), false, "the run counts from the break");
  assert.equal(pressAndClick(gate, 500 + SEEN_MS + 1), true);

  // A break also puts the pointer outside: it must come in again.
  const off = pointedGate();
  off.observe(seen(400, { isVisible: false }));
  off.observe(seen(500));
  // Without a break the pointer would have been in since 0; now it counts from 1000.
  off.pointerInside(true, 1000, true);
  assert.equal(pressAndClick(off, 500 + SEEN_MS + 1), false, "in for under DWELL_MS since the break");
  assert.equal(pressAndClick(off, 1000 + DWELL_MS), true);
});

test("a press counts for one click only", () => {
  const gate = pointedGate();
  assert.equal(pressAndClick(gate, T), true);
  gate.pressUsed();
  assert.equal(gate.clickCounts(real(T + 200)), false, "Enter afterwards, no new press");
});

test("a Yes is refused without pointer movement, or with an untrusted one", () => {
  const still = new SeenGate();
  still.observe(seen(0));
  still.pointerInside(true, 0, false);
  assert.equal(pressAndClick(still, 5_000), false);

  const fake = new SeenGate();
  fake.observe(seen(0));
  fake.pointerInside(false, 0, false);
  fake.pointerInside(false, 10, true);
  assert.equal(pressAndClick(fake, 5_000), false);
});

test("a frame slid under the pointer between a double-click's two clicks doesn't count", () => {
  // Seen for minutes, pointed at long ago, then the pointer left.
  const gate = pointedGate();
  gate.pointerGone();
  // The frame moves under the resting pointer: it enters now, jitters, and the second click lands.
  gate.pointerInside(true, 60_000, false);
  gate.pointerInside(true, 60_002, true);
  assert.equal(pressAndClick(gate, 60_000 + 250), false);
  assert.equal(pressAndClick(gate, 60_000 + DWELL_MS - 1), false);
  assert.equal(pressAndClick(gate, 60_000 + DWELL_MS), true, "a pointer that rests there long enough counts");
});

test("the frame losing focus, or the pointer leaving, starts the pointer's part again", () => {
  const gate = pointedGate();
  gate.pointerGone();
  assert.equal(pressAndClick(gate, 5_000), false);
});

test("a press must begin after it armed, on Yes, trusted; a click alone (a key) never counts", () => {
  const early = pointedGate();
  early.pressed(real(T - DWELL_MS - 300));
  assert.equal(early.clickCounts(real(T + 50)), false, "pressed before it armed, released after");

  assert.equal(pressAndClick(pointedGate(), T, { onYes: false }), false, "pressed elsewhere");
  assert.equal(pressAndClick(pointedGate(), T, { isTrusted: false }), false);
  assert.equal(pressAndClick(pointedGate(), T, {}, { onYes: false }), false, "released elsewhere");
  assert.equal(pressAndClick(pointedGate(), T, {}, { isTrusted: false }), false);
  assert.equal(pointedGate().clickCounts(real(T)), false, "Enter on a focused Yes: no press");
});

test("still seen after the click only in the same run, with the pointer still in", () => {
  const ok = pointedGate();
  assert.equal(pressAndClick(ok, T), true);
  assert.equal(ok.stillSeen(T + SETTLE_MS), true);

  const covered = pointedGate();
  pressAndClick(covered, T);
  covered.observe(seen(T + 50, { isVisible: false }));
  covered.observe(seen(T + 150));
  assert.equal(covered.stillSeen(T + SETTLE_MS), false, "a break and a new run is not the same run");

  const left = pointedGate();
  pressAndClick(left, T);
  left.pointerGone();
  assert.equal(left.stillSeen(T + SETTLE_MS), false);
});

test("only an observer that reports visibility counts as v2", () => {
  class IO {}
  assert.equal(tracksVisibility({ IntersectionObserver: IO, IntersectionObserverEntry: { prototype: { isVisible: false } } }), true);
  assert.equal(tracksVisibility({ IntersectionObserver: IO, IntersectionObserverEntry: { prototype: {} } }), false);
  assert.equal(tracksVisibility({ IntersectionObserver: IO }), false);
  assert.equal(tracksVisibility({}), false);
  assert.equal(tracksVisibility(null), false);
});

test("without v2 nothing is watched, so nothing can arm", () => {
  class IO {}
  const gate = new SeenGate();
  const win = { IntersectionObserver: IO, IntersectionObserverEntry: { prototype: {} } };
  assert.equal(watchPanel(win, {}, () => true, gate, () => {}), null);
  assert.equal(pressAndClick(gate, 5_000), false);
});

/** A fake window and panel: a clock, the observer, timers and listeners. */
function fakeWorld() {
  const world = { clock: 0, listeners: {}, winListeners: {}, timers: [], observer: null };
  class IO {
    constructor(cb, options) {
      world.observer = { cb, options, observed: [], pending: [], stopped: false };
    }
    observe(el) {
      world.observer.observed.push(el);
    }
    disconnect() {
      world.observer.stopped = true;
    }
    takeRecords() {
      const out = world.observer.pending;
      world.observer.pending = [];
      return out;
    }
  }
  const on = (table) => ({
    addEventListener: (type, fn) => {
      table[type] = fn;
    },
    removeEventListener: (type) => {
      delete table[type];
    },
  });
  world.win = {
    IntersectionObserver: IO,
    IntersectionObserverEntry: { prototype: { isVisible: true } },
    performance: { now: () => world.clock },
    setTimeout: (fn, ms) => world.timers.push({ fn, at: world.clock + ms }),
    ...on(world.winListeners),
  };
  world.panel = on(world.listeners);
  world.yes = { button: true };
  world.fire = (type, event = {}) => world.listeners[type]({ isTrusted: true, target: world.yes, ...event });
  world.runTimers = async (until) => {
    world.clock = until;
    for (const t of world.timers.splice(0)) t.fn();
    await new Promise((resolve) => setImmediate(resolve));
  };
  return world;
}

test("with v2 the panel and the pointer are watched, and a click is judged again after it settles", async () => {
  const world = fakeWorld();
  const gate = new SeenGate();
  let changes = 0;
  const watch = watchPanel(world.win, world.panel, (t) => t === world.yes, gate, () => changes++);
  assert.deepEqual(world.observer.options, { threshold: [1.0], trackVisibility: true, delay: OBSERVE_DELAY_MS });
  assert.deepEqual(world.observer.observed, [world.panel]);
  world.observer.cb([seen(0)]);
  world.fire("pointerenter");
  world.clock = 10;
  world.fire("pointermove");
  world.clock = T;
  assert.equal(watch.armed(), true);
  world.fire("pointerdown");
  const judged = watch.judge({ isTrusted: true, target: world.yes });
  await world.runTimers(T + SETTLE_MS);
  assert.equal(await judged, true);
  assert.ok(changes >= 1);

  // Covered just after the click, not yet delivered: the second look takes it and refuses.
  world.clock = T + 1000;
  world.fire("pointerdown");
  const second = watch.judge({ isTrusted: true, target: world.yes });
  world.observer.pending.push(seen(T + 1010, { isVisible: false }));
  await world.runTimers(T + 1000 + SETTLE_MS);
  assert.equal(await second, false);

  // The press is used up: a second click with no new press doesn't count.
  assert.equal(await watch.judge({ isTrusted: true, target: world.yes }), false);
  // A press released off Yes counts for nothing.
  world.clock = T + 3000;
  world.fire("pointerdown");
  world.fire("pointerup", { target: {} });
  assert.equal(await watch.judge({ isTrusted: true, target: world.yes }), false);

  watch.stop();
  assert.equal(world.observer.stopped, true);
  assert.deepEqual(Object.keys(world.listeners), []);
  assert.deepEqual(Object.keys(world.winListeners), []);
});

test("the frame losing focus disarms it", () => {
  const world = fakeWorld();
  const gate = new SeenGate();
  const watch = watchPanel(world.win, world.panel, (t) => t === world.yes, gate, () => {});
  world.observer.cb([seen(0)]);
  world.fire("pointerenter");
  world.fire("pointermove");
  world.clock = T;
  assert.equal(watch.armed(), true);
  world.winListeners.blur();
  assert.equal(watch.armed(), false);
});

test("the in-frame confirm opens no window, is judged by the gate, and offers this app's own page", () => {
  const src = read("../src/components/embed/FrameConfirm.tsx");
  assert.doesNotMatch(src, /window\.open\(/);
  assert.match(src, /watchPanel\(/);
  assert.match(src, /if \(judged && \(await judged\)\) finish\(true\);/);
  assert.match(src, /target="_top"/);
});

test("the host's colours and font never reach a Yes asked in a frame", () => {
  const css = read("../src/app/globals.css");
  // The app's own tokens are declared again on .consent, for light and dark.
  assert.match(css, /:root\[data-theme="dark"\],\s*\[data-theme="dark"\] \.consent \{/);
  assert.match(css, /:root\[data-theme="light"\],\s*\[data-theme="light"\] \.consent \{/);
  // On every element inside too, so a heading never takes the host's display face.
  assert.match(css, /\.consent,\s*\.consent \* \{\s*font-family: system-ui/);
  // Every token a host may set is among them.
  const embed = read("../src/lib/embed.ts");
  const tokens = [...embed.match(/THEME_TOKENS = \[([^\]]*)\]/)[1].matchAll(/"([a-z-]+)"/g)].map((m) => m[1]);
  const light = css.slice(css.indexOf('[data-theme="light"] .consent'));
  for (const token of tokens) assert.match(light, new RegExp(`--${token}:`), token);
  // And the place that asks wears it.
  assert.match(read("../src/components/embed/FrameConfirm.tsx"), /className="consent /);
});
