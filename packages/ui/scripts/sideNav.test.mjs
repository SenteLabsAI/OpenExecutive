import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { nextSideNavOpen } from "../src/lib/sideNav.ts";

test("the bar button toggles the menu", () => {
  assert.equal(nextSideNavOpen(false, { type: "toggle" }), true);
  assert.equal(nextSideNavOpen(true, { type: "toggle" }), false);
});

test("tapping an item closes the menu, even the item already selected", () => {
  assert.equal(nextSideNavOpen(true, { type: "menu-click", closesNav: true }), false);
});

test("other taps inside the menu keep it open", () => {
  assert.equal(nextSideNavOpen(true, { type: "menu-click", closesNav: false }), true);
});

test("a tap on the page behind, Escape, or a new selection closes it", () => {
  assert.equal(nextSideNavOpen(true, { type: "outside" }), false);
  assert.equal(nextSideNavOpen(true, { type: "key", key: "Escape" }), false);
  assert.equal(nextSideNavOpen(true, { type: "selection-changed" }), false);
});

test("other keys leave the menu as it is", () => {
  assert.equal(nextSideNavOpen(true, { type: "key", key: "Tab" }), true);
  assert.equal(nextSideNavOpen(false, { type: "key", key: "Escape" }), false);
});

// Every page that hands its items to PageSideNav marks them, or re-tapping
// the current item would leave the menu covering the page.
for (const page of ["app/guide/page.tsx", "app/architecture/page.tsx", "app/council/page.tsx"]) {
  test(`${page} marks its menu items data-closes-nav`, () => {
    const src = readFileSync(new URL(`../src/${page}`, import.meta.url), "utf8");
    assert.match(src, /<PageSideNav/);
    assert.match(src, /data-closes-nav/);
  });
}
