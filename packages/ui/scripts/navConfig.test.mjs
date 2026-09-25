import assert from "node:assert/strict";
import test from "node:test";
import { buildMobilePrimary, buildPrimaryNav } from "../src/components/shell/navConfig.ts";

const shape = (groups) =>
  groups.map((g) => ({ key: g.key, label: g.label, items: g.items.map((i) => `${i.label} → ${i.href}`) }));

// Pinned so a solo change can never leak into what a team install shows.
const TEAM = [
  { key: "workspace", label: "Workspace", items: ["Workflows → /jobs", "Documents → /artifacts", "Watch list → /watchlist"] },
  {
    key: "company",
    label: "Company",
    items: ["Departments → /departments", "People → /people", "Company profile → /company-profile"],
  },
  { key: "knowledge", label: "Knowledge", items: ["Knowledge base → /knowledge"] },
];

test("team nav is unchanged, and team is the default", () => {
  assert.deepEqual(shape(buildPrimaryNav()), TEAM);
  assert.deepEqual(buildPrimaryNav({ mode: "team" }), buildPrimaryNav());
  const notOnboarded = shape(buildPrimaryNav({ isOnboarded: false }));
  assert.deepEqual(notOnboarded[1].items, ["Departments → /departments", "People → /people", "Set up company → /onboard"]);
});

test("solo swaps the Company group for You: Goals, People, Business profile", () => {
  const solo = shape(buildPrimaryNav({ mode: "solo" }));
  assert.deepEqual(solo[1], {
    key: "you",
    label: "You",
    items: ["Goals → /goals", "People → /people", "Business profile → /company-profile"],
  });
  // Everything around it is the team nav, unchanged.
  assert.deepEqual([solo[0], solo[2]], [TEAM[0], TEAM[2]]);
  assert.ok(!JSON.stringify(solo).includes("/departments"));
  const notOnboarded = shape(buildPrimaryNav({ mode: "solo", isOnboarded: false }));
  assert.equal(notOnboarded[1].items[2], "Set up your business → /onboard");
});

test("the review badge rides along in both modes", () => {
  for (const mode of ["team", "solo"]) {
    const kb = buildPrimaryNav({ mode, reviewBadge: 4 }).at(-1).items[0];
    assert.equal(kb.badge, 4);
  }
});

test("mobile bar: solo swaps People for Goals", () => {
  const team = buildMobilePrimary().map((i) => i.href);
  assert.deepEqual(team, ["/", "/memories", "/?new=1", "/people", "/jobs"]);
  assert.deepEqual(buildMobilePrimary("team"), buildMobilePrimary());
  assert.deepEqual(buildMobilePrimary("solo").map((i) => i.href), ["/", "/memories", "/?new=1", "/goals", "/jobs"]);
});

test("every destination explains itself", () => {
  for (const mode of ["team", "solo"]) {
    const items = [...buildPrimaryNav({ mode }).flatMap((g) => g.items), ...buildMobilePrimary(mode)];
    for (const item of items) assert.ok(item.description.trim(), `${item.href} needs a description`);
  }
});
