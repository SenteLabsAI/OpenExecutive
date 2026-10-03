import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
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

// Every PageSideNav user marks its menu items, or re-tapping the current item
// would leave the menu covering the page. Found by scanning src/, so a new
// user is checked without editing this list; the menu items live in the file
// itself or in the one component it hands them to.
const MENU_ITEMS_IN = { "components/knowledge/KnowledgeWorkspace.tsx": "components/knowledge/SourceTree.tsx" };

function sourceFiles(dir) {
  return readdirSync(new URL(`../src/${dir}`, import.meta.url), { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? sourceFiles(`${dir}${e.name}/`) : /\.tsx$/.test(e.name) ? [`${dir}${e.name}`] : []
  );
}
const read = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), "utf8");
const users = sourceFiles("").filter((f) => f !== "components/shell/PageSideNav.tsx" && /<PageSideNav/.test(read(f)));

test("PageSideNav users are found", () => {
  assert.ok(users.length >= 3, `found ${users.join(", ")}`);
});

for (const user of users) {
  test(`${user} marks its menu items data-closes-nav`, () => {
    assert.match(read(MENU_ITEMS_IN[user] ?? user), /data-closes-nav/);
  });
}
