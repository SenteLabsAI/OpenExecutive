import assert from "node:assert/strict";
import test from "node:test";
import { SETTINGS_GROUPS, SETTINGS_PAGES } from "../src/components/shell/navConfig.ts";
import { firstNavHref, isNavItemActive, settingsNav } from "../src/lib/settingsNav.ts";

// The Settings menu beside every page (lib/settingsNav).
const menu = (groups) => groups.map((g) => [g.label, g.items.map((i) => `${i.label} → ${i.href}`)]);
const find = (groups, key) => groups.flatMap((g) => g.items).find((i) => i.key === key);

test("the menu lists the hub's pages in the hub's groups", () => {
  const groups = settingsNav(SETTINGS_PAGES, SETTINGS_GROUPS);
  assert.deepEqual(menu(groups), [
    [
      "You and your Executive",
      [
        "Your Executive → /settings/executive",
        "Notifications → /settings/notifications",
        "About you → /settings/memory",
      ],
    ],
    ["Company", ["Workspace → /settings/workspace"]],
    ["More", ["Advanced → /settings/advanced", "About → /settings/about"]],
  ]);
  assert.equal(firstNavHref(groups), "/settings/executive");
  assert.equal(firstNavHref([]), null);
});

test("a page the person can't open isn't listed", () => {
  const groups = settingsNav(SETTINGS_PAGES.filter((p) => p.id !== "memory" && p.id !== "notifications"), SETTINGS_GROUPS);
  assert.deepEqual(menu(groups)[0], ["You and your Executive", ["Your Executive → /settings/executive"]]);
});

test("each entry lights on its own pages", () => {
  const groups = settingsNav(SETTINGS_PAGES, SETTINGS_GROUPS);
  const executive = find(groups, "executive");
  assert.ok(isNavItemActive(executive, "/settings/executive"));
  assert.ok(isNavItemActive(executive, "/settings/on-its-own"));
  assert.ok(!isNavItemActive(executive, "/settings/executive-x"));
  const advanced = find(groups, "advanced");
  for (const path of ["/settings/advanced", "/settings/tools", "/settings/status"]) {
    assert.ok(isNavItemActive(advanced, path), path);
  }
  assert.ok(!isNavItemActive(find(groups, "workspace"), "/settings"));
});
