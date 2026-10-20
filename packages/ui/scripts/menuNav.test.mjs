import assert from "node:assert/strict";
import test from "node:test";
import { nextMenuIndex } from "../src/lib/menuNav.ts";

const none = [false, false, false];

test("arrow down from the button lands on the first item", () => {
  assert.equal(nextMenuIndex(-1, "ArrowDown", none), 0);
});

test("arrow up from the button lands on the last item", () => {
  assert.equal(nextMenuIndex(-1, "ArrowUp", none), 2);
});

test("arrows wrap around", () => {
  assert.equal(nextMenuIndex(2, "ArrowDown", none), 0);
  assert.equal(nextMenuIndex(0, "ArrowUp", none), 2);
});

test("disabled items are skipped", () => {
  assert.equal(nextMenuIndex(0, "ArrowDown", [false, true, false]), 2);
  assert.equal(nextMenuIndex(-1, "ArrowDown", [true, false, false]), 1);
  assert.equal(nextMenuIndex(-1, "End", [false, false, true]), 1);
});

test("Home and End go to the first and last item", () => {
  assert.equal(nextMenuIndex(1, "Home", none), 0);
  assert.equal(nextMenuIndex(1, "End", none), 2);
});

test("other keys and empty menus leave focus where it is", () => {
  assert.equal(nextMenuIndex(1, "Tab", none), 1);
  assert.equal(nextMenuIndex(-1, "ArrowDown", []), -1);
  assert.equal(nextMenuIndex(-1, "ArrowDown", [true, true]), -1);
});
