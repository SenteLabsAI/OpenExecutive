import assert from "node:assert/strict";
import test from "node:test";
import {
  ADVANCED_GROUPS,
  ADVANCED_ITEMS,
  PROFILE_NAV,
  SETTINGS_PAGES,
  advancedItemsByGroup,
  buildDestinations,
  buildHubs,
  buildMobilePrimary,
  hubForPath,
  isDestinationActive,
  profileWording,
  settingsPageForHash,
} from "../src/components/shell/navConfig.ts";

const links = (items) => items.map((i) => `${i.label} → ${i.href}`);
const ROLE_KINDS = ["owner", "in_house", "independent", "other", null, undefined];

test("the main menu is six places, team is the default", () => {
  assert.deepEqual(links(buildDestinations()), [
    "Home → /",
    "Chats → /chats",
    "Work → /jobs",
    "Company → /people",
    "Knowledge → /knowledge",
    "Pulse → /memories",
  ]);
  assert.deepEqual(buildDestinations({ mode: "team" }), buildDestinations());
});

test("solo swaps Company for You, which opens on Goals", () => {
  const solo = links(buildDestinations({ mode: "solo", roleKind: "owner" }));
  assert.equal(solo[3], "You → /goals");
  assert.equal(solo.length, 6);
});

test("team hubs: Work and Company tabs", () => {
  const [work, company] = buildHubs();
  assert.deepEqual(links(work.tabs), ["Workflows → /jobs", "Documents → /artifacts", "Watch list → /watchlist"]);
  assert.deepEqual(links(company.tabs), [
    "People → /people",
    "Goals → /goals",
    "Departments → /departments",
    "Company profile → /company-profile",
  ]);
  const notOnboarded = buildHubs({ isOnboarded: false })[1].tabs.at(-1);
  assert.deepEqual([notOnboarded.label, notOnboarded.href], ["Set up company", "/onboard"]);
  assert.equal(
    company.tabs.at(-1).description,
    "Your company's identity and strategy — set up once, edited any time.",
  );
});

test("team hubs ignore the role", () => {
  for (const roleKind of ROLE_KINDS) {
    assert.deepEqual(buildHubs({ roleKind }), buildHubs());
    assert.deepEqual(buildDestinations({ roleKind }), buildDestinations());
  }
});

test("solo: You holds Goals, People and the profile, and no Departments", () => {
  const you = buildHubs({ mode: "solo", roleKind: "owner" })[1];
  assert.equal(you.key, "you");
  assert.deepEqual(links(you.tabs), ["Goals → /goals", "People → /people", "Business profile → /company-profile"]);
  assert.ok(!JSON.stringify(buildHubs({ mode: "solo" })).includes("/departments"));
  const notOnboarded = buildHubs({ mode: "solo", roleKind: "owner", isOnboarded: false })[1].tabs[2];
  assert.deepEqual([notOnboarded.label, notOnboarded.href], ["Set up your business", "/onboard"]);
});

test("solo: any other role, or none, is 'Your work'", () => {
  // null covers an unset role and one GET /workspace hides from a non-principal.
  for (const roleKind of ["in_house", "independent", "other", null, undefined]) {
    const item = buildHubs({ mode: "solo", roleKind })[1].tabs[2];
    assert.deepEqual([item.label, item.href], ["Your work", "/company-profile"], String(roleKind));
    assert.equal(item.description, "Your work — the organisation you work in, who it serves, your priorities.");
    assert.ok(!/business|company/i.test(item.label + item.description), String(roleKind));
    const notOnboarded = buildHubs({ mode: "solo", roleKind, isOnboarded: false })[1].tabs[2];
    assert.deepEqual([notOnboarded.label, notOnboarded.href], ["Set up your work", "/onboard"]);
  }
  assert.deepEqual(buildHubs({ mode: "solo" }), buildHubs({ mode: "solo", roleKind: null }));
});

test("a hub's menu entry stays lit on every one of its tabs", () => {
  const dests = buildDestinations();
  const lit = (path) => dests.filter((d) => isDestinationActive(d, path)).map((d) => d.key);
  assert.deepEqual(lit("/"), ["home"]);
  assert.deepEqual(lit("/jobs/runs/42"), ["work"]);
  assert.deepEqual(lit("/artifacts/7"), ["work"]);
  assert.deepEqual(lit("/watchlist"), ["work"]);
  assert.deepEqual(lit("/goals"), ["company"]);
  assert.deepEqual(lit("/departments/finance"), ["company"]);
  assert.deepEqual(lit("/company-profile"), ["company"]);
  assert.deepEqual(lit("/settings"), []);
});

test("hubForPath finds the hub a page belongs to", () => {
  assert.equal(hubForPath("/jobs/new")?.key, "work");
  assert.equal(hubForPath("/people/3")?.key, "company");
  assert.equal(hubForPath("/goals", { mode: "solo" })?.key, "you");
  assert.equal(hubForPath("/departments", { mode: "solo" }), null);
  assert.equal(hubForPath("/settings"), null);
  assert.equal(hubForPath("/"), null);
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

test("the review badge rides on Knowledge in both modes", () => {
  for (const mode of ["team", "solo"]) {
    const kb = buildDestinations({ mode, reviewBadge: 4 }).find((d) => d.key === "knowledge");
    assert.equal(kb.badge, 4);
  }
});

test("phone bar: five, with New chat in the middle", () => {
  assert.deepEqual(buildMobilePrimary().map((i) => i.href), ["/", "/chats", "/?new=1", "/jobs", "/people"]);
  assert.deepEqual(buildMobilePrimary({ mode: "solo" }).map((i) => i.href), ["/", "/chats", "/?new=1", "/jobs", "/goals"]);
});

test("every destination and tab explains itself", () => {
  for (const mode of ["team", "solo"]) {
    for (const roleKind of ROLE_KINDS) {
      const items = [
        ...buildDestinations({ mode, roleKind }),
        ...buildHubs({ mode, roleKind }).flatMap((h) => [h, ...h.tabs]),
        ...buildMobilePrimary({ mode, roleKind }),
      ];
      for (const item of items) assert.ok(item.description.trim(), `${item.label} needs a description`);
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

test("the Settings hub: one tile per page, each with its own route", () => {
  assert.deepEqual(
    SETTINGS_PAGES.map((p) => `${p.label} → ${p.href}`),
    [
      "Your Executive → /settings/executive",
      "Act as me → /settings/act-as-me",
      "Workspace → /settings/workspace",
      "Advanced → /settings/advanced",
      "About → /settings/about",
    ],
  );
  assert.equal(new Set(SETTINGS_PAGES.map((p) => p.id)).size, SETTINGS_PAGES.length);
  for (const p of SETTINGS_PAGES) {
    assert.ok(p.label.trim() && p.description.trim(), p.id);
    assert.ok(p.href.startsWith("/settings/"), p.id);
  }
  // Setup status keeps its own route, outside the tiles.
  assert.ok(!SETTINGS_PAGES.some((p) => p.href === "/settings/status"));
});

test("old /settings#anchors land on the matching page", () => {
  const to = (hash) => settingsPageForHash(hash)?.href ?? null;
  assert.equal(to("#executive"), "/settings/executive");
  assert.equal(to("#workspace"), "/settings/workspace");
  assert.equal(to("act-as-me"), "/settings/act-as-me");
  assert.equal(to("#tools"), "/settings/advanced");
  for (const g of ADVANCED_GROUPS) assert.equal(to(`#tools-${g.key}`), "/settings/advanced");
  assert.equal(to("#about"), "/settings/about");
  assert.equal(to(""), null);
  assert.equal(to("#"), null);
  assert.equal(to("#nope"), null);
  assert.equal(to("#%E0%A4%A"), null);
  // Every anchor belongs to exactly one page.
  const all = SETTINGS_PAGES.flatMap((p) => p.hashes);
  assert.equal(new Set(all).size, all.length);
});
