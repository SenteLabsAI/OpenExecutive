import assert from "node:assert/strict";
import test from "node:test";
import {
  ADVANCED_GROUPS,
  ADVANCED_ITEMS,
  PROFILE_NAV,
  SETTINGS_SECTIONS,
  advancedItemsByGroup,
  buildMobilePrimary,
  buildPrimaryNav,
  profileWording,
} from "../src/components/shell/navConfig.ts";

const shape = (groups) =>
  groups.map((g) => ({ key: g.key, label: g.label, items: g.items.map((i) => `${i.label} → ${i.href}`) }));

// Pinned so a solo change can never leak into what a team install shows.
const TEAM = [
  { key: "workspace", label: "Workspace", items: ["Workflows → /jobs", "Documents → /artifacts", "Watch list → /watchlist"] },
  {
    key: "company",
    label: "Company",
    items: ["Departments → /departments", "Goals → /goals", "People → /people", "Company profile → /company-profile"],
  },
  { key: "knowledge", label: "Knowledge", items: ["Knowledge base → /knowledge"] },
];

const ROLE_KINDS = ["owner", "in_house", "independent", "other", null, undefined];

test("team nav is unchanged, and team is the default", () => {
  assert.deepEqual(shape(buildPrimaryNav()), TEAM);
  assert.deepEqual(buildPrimaryNav({ mode: "team" }), buildPrimaryNav());
  const notOnboarded = shape(buildPrimaryNav({ isOnboarded: false }));
  assert.deepEqual(notOnboarded[1].items, [
    "Departments → /departments",
    "Goals → /goals",
    "People → /people",
    "Set up company → /onboard",
  ]);
  const profile = buildPrimaryNav()[1].items[3];
  assert.equal(profile.description, "Your company's identity and strategy — set up once, edited any time.");
});

test("team nav ignores the role", () => {
  for (const roleKind of ROLE_KINDS) {
    assert.deepEqual(buildPrimaryNav({ roleKind }), buildPrimaryNav());
    assert.deepEqual(buildPrimaryNav({ roleKind, isOnboarded: false }), buildPrimaryNav({ isOnboarded: false }));
  }
});

test("solo swaps the Company group for You: Goals, People and the profile", () => {
  const solo = shape(buildPrimaryNav({ mode: "solo", roleKind: "owner" }));
  assert.deepEqual(solo[1], {
    key: "you",
    label: "You",
    items: ["Goals → /goals", "People → /people", "Business profile → /company-profile"],
  });
  // Everything around it is the team nav, unchanged.
  assert.deepEqual([solo[0], solo[2]], [TEAM[0], TEAM[2]]);
  assert.ok(!JSON.stringify(solo).includes("/departments"));
});

test("solo: an owner's profile is their business", () => {
  const item = buildPrimaryNav({ mode: "solo", roleKind: "owner" })[1].items[2];
  assert.equal(item.label, "Business profile");
  assert.equal(item.description, "Your business — what you offer, who you serve, your priorities.");
  const notOnboarded = buildPrimaryNav({ mode: "solo", roleKind: "owner", isOnboarded: false })[1].items[2];
  assert.deepEqual([notOnboarded.label, notOnboarded.href], ["Set up your business", "/onboard"]);
});

test("solo: any other role, or none, is 'Your work'", () => {
  // null covers an unset role and one GET /workspace hides from a non-principal.
  for (const roleKind of ["in_house", "independent", "other", null, undefined]) {
    const item = buildPrimaryNav({ mode: "solo", roleKind })[1].items[2];
    assert.deepEqual([item.label, item.href], ["Your work", "/company-profile"], String(roleKind));
    assert.equal(item.description, "Your work — the organisation you work in, who it serves, your priorities.");
    assert.ok(!/business|company/i.test(item.label + item.description), String(roleKind));
    const notOnboarded = buildPrimaryNav({ mode: "solo", roleKind, isOnboarded: false })[1].items[2];
    assert.deepEqual([notOnboarded.label, notOnboarded.href], ["Set up your work", "/onboard"]);
  }
  // The default (no roleKind passed) is the same as an unset role.
  assert.deepEqual(buildPrimaryNav({ mode: "solo" }), buildPrimaryNav({ mode: "solo", roleKind: null }));
});

test("profileWording: company for a team, business for a solo owner, work otherwise", () => {
  for (const roleKind of ROLE_KINDS) {
    assert.equal(profileWording("team", roleKind), "company");
  }
  assert.equal(profileWording(), "company");
  assert.equal(profileWording("solo", "owner"), "business");
  for (const roleKind of ["in_house", "independent", "other", null, undefined]) {
    assert.equal(profileWording("solo", roleKind), "work");
  }
  // Every wording has a label, a setup label and a description.
  for (const [wording, copy] of Object.entries(PROFILE_NAV)) {
    for (const [key, text] of Object.entries(copy)) assert.ok(text.trim(), `${wording}.${key}`);
  }
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
    for (const roleKind of ROLE_KINDS) {
      const items = [...buildPrimaryNav({ mode, roleKind }).flatMap((g) => g.items), ...buildMobilePrimary(mode)];
      for (const item of items) assert.ok(item.description.trim(), `${item.href} needs a description`);
    }
  }
});

test("every Settings tool is in exactly one group, and no group is empty", () => {
  const keys = ADVANCED_GROUPS.map((g) => g.key);
  for (const item of ADVANCED_ITEMS) {
    assert.ok(keys.includes(item.group), `${item.href} has group ${item.group}`);
    assert.ok(item.description.trim(), `${item.href} needs a description`);
  }
  assert.equal(new Set(ADVANCED_ITEMS.map((i) => i.href)).size, ADVANCED_ITEMS.length);
  for (const group of advancedItemsByGroup()) assert.ok(group.items.length > 0, group.key);
});

test("the Settings tools are grouped by what you'd use them for", () => {
  const grouped = advancedItemsByGroup().map((g) => ({
    key: g.key,
    label: g.label,
    items: g.items.map((i) => `${i.label} → ${i.href}`),
  }));
  assert.deepEqual(grouped, [
    {
      key: "diagnose",
      label: "Check & diagnose",
      items: ["Setup status → /settings/status", "Audit log → /audit", "Token usage → /audit/usage"],
    },
    {
      key: "configure",
      label: "Configure",
      items: ["Agent Council → /council", "Company Simulator → /demo", "Client Companies → /clients"],
    },
    { key: "learn", label: "Learn", items: ["User Guide → /guide", "Architecture → /architecture"] },
  ]);
  // Grouping reorders nothing and drops nothing.
  assert.equal(grouped.flatMap((g) => g.items).length, ADVANCED_ITEMS.length);
});

test("the Settings sections have unique ids and a label each", () => {
  const ids = SETTINGS_SECTIONS.map((s) => s.id);
  assert.deepEqual(ids, ["executive", "workspace", "act-as-me", "tools", "about"]);
  assert.equal(new Set(ids).size, ids.length);
  for (const s of SETTINGS_SECTIONS) assert.ok(s.label.trim(), s.id);
});
