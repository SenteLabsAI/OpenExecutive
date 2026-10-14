import assert from "node:assert/strict";
import test from "node:test";
import {
  checklistComplete,
  checklistProgress,
  firstQuestion,
  gettingStartedItems,
  OWN_MAILBOX_SENDS,
  shouldRedirectToOnboard,
} from "../src/lib/firstRun.ts";

const check = (id, state) => ({ id, label: id, state, summary: "", fix: null, link: null });

test("the owner without a profile is sent to setup, once both answers are in", () => {
  assert.equal(shouldRedirectToOnboard({ profileLoaded: false, isPrincipal: true, later: false }), true);
  assert.equal(shouldRedirectToOnboard({ profileLoaded: undefined, isPrincipal: true, later: false }), false);
  assert.equal(shouldRedirectToOnboard({ profileLoaded: false, isPrincipal: undefined, later: false }), false);
});

test("a profile, a teammate, or 'later' means no redirect", () => {
  assert.equal(shouldRedirectToOnboard({ profileLoaded: true, isPrincipal: true, later: false }), false);
  assert.equal(shouldRedirectToOnboard({ profileLoaded: false, isPrincipal: false, later: false }), false);
  assert.equal(shouldRedirectToOnboard({ profileLoaded: false, isPrincipal: true, later: true }), false);
});

const base = {
  profileLoaded: true,
  companyName: "Northwind Supply",
  hasAsked: false,
  checks: [check("gmail", "off"), check("your_gmail", "off")],
  team: false,
  teamSize: 1,
};

test("the rows, their state and the heading count", () => {
  const items = gettingStartedItems(base);
  assert.deepEqual(
    items.map((i) => [i.id, i.done, i.optional]),
    [
      ["profile", true, false],
      ["ask", false, false],
      ["exec_mailbox", false, false],
      ["own_mailbox", false, true],
    ],
  );
  assert.equal(items[0].detail, "Northwind Supply · you are the owner");
  assert.deepEqual(checklistProgress(items), { done: 1, total: 3 });
  assert.equal(checklistComplete(items), false);
});

test("the two mailboxes are separate rows read from separate checks", () => {
  const items = gettingStartedItems({
    ...base,
    checks: [check("gmail", "ok"), check("your_gmail", "warn")],
  });
  assert.equal(items.find((i) => i.id === "exec_mailbox").done, true);
  assert.equal(items.find((i) => i.id === "own_mailbox").done, false);
});

test("while the checks are loading, nothing is marked done", () => {
  const items = gettingStartedItems({ ...base, checks: null });
  assert.equal(items.find((i) => i.id === "exec_mailbox").done, false);
});

test("a team adds the roster row, and the optional row never blocks completion", () => {
  const items = gettingStartedItems({
    ...base,
    hasAsked: true,
    checks: [check("gmail", "ok")],
    team: true,
    teamSize: 3,
  });
  assert.equal(items.at(-1).id, "team");
  assert.equal(items.at(-1).done, true);
  assert.equal(checklistComplete(items), true);
  assert.deepEqual(checklistProgress(items), { done: 4, total: 4 });
});

test("the first question comes from what setup saved", () => {
  assert.equal(
    firstQuestion({ current_year: ["  ", "Launch same-day delivery.", "Hire a CFO"], north_star_metric: "NRR" }, false),
    'Where should I start this week on "Launch same-day delivery"?',
  );
  assert.equal(firstQuestion({ current_year: [], north_star_metric: "NRR above 110%" }, true),
    'Where should I start this week on "NRR above 110%"?');
  assert.equal(firstQuestion(null, true), "What should I focus on this week?");
  assert.equal(firstQuestion({ current_year: [] }, false), "What should the team focus on this week?");
  assert.ok(firstQuestion({ current_year: ["x".repeat(500)] }, true).length < 220);
});

test("the mailbox row says when it sends", () => {
  const items = gettingStartedItems(base);
  const own = items.find((i) => i.id === "own_mailbox");
  assert.ok(own.detail.includes(OWN_MAILBOX_SENDS));
  assert.ok(!/never sends/i.test(own.detail));
});
