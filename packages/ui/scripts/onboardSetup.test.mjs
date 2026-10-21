import assert from "node:assert/strict";
import test from "node:test";
import {
  afterSaveSteps,
  checkNotes,
  goalsToSave,
  inferredMode,
  modeLine,
  understoodRole,
} from "../src/lib/onboardSetup.ts";

const profile = (over = {}) => ({
  competitive_landscape: { primary_competitors: [], competitive_advantages: [] },
  vendors: [],
  tickers: [],
  ...over,
});
const goal = (title, owner_name = "", department_title = "") => ({ title, target: "", owner_name, department_title });

test("setup picks Personal unless the text says Team", () => {
  assert.equal(inferredMode(null), "solo");
  assert.equal(inferredMode({ mode: null }), "solo");
  assert.equal(inferredMode({ mode: "team" }), "team");
});

test("only the role fields the text gave are saved", () => {
  assert.deepEqual(understoodRole(null), {});
  assert.deepEqual(understoodRole({ role_kind: "owner", role_title: "", reports_to: null }), { role_kind: "owner" });
  assert.deepEqual(understoodRole({ role_kind: null, role_title: "COO", reports_to: "CEO" }), {
    role_title: "COO",
    reports_to: "CEO",
  });
});

test("the mode line names the team, up to four", () => {
  assert.equal(modeLine("solo", ["Rufus", "Dan"]), "Set up just for you");
  assert.equal(modeLine("team", []), "Set up for your team");
  assert.equal(modeLine("team", ["Rufus", " ", "Dan"]), "Set up for your team: Rufus, Dan");
  assert.equal(
    modeLine("team", ["A", "B", "C", "D", "E", "F"]),
    "Set up for your team: A, B, C, D and 2 more",
  );
});

test("notes that speak in field names never reach the review", () => {
  assert.deepEqual(
    checkNotes(["annual_revenue_arr not stated", "  ", "I guessed your competitors from your site."]),
    ["I guessed your competitors from your site."],
  );
});

test("after save always researches, and mentions goals and watched services when there are some", () => {
  assert.deepEqual(afterSaveSteps(profile(), []), [
    "Research your competitors and suggest a shortlist in your brief",
  ]);
  const steps = afterSaveSteps(
    profile({ competitive_landscape: { primary_competitors: ["Acme"] }, vendors: ["Fly.io"], tickers: ["NET"] }),
    [goal("Launch")],
  );
  assert.equal(steps.length, 3);
  assert.match(steps[0], /market and competitors/);
  assert.match(steps[1], /every week/);
  assert.equal(steps[2], "Watch the status pages and news for Fly.io, NET");
});

test("goals drop blanks and owners or departments no longer on the review", () => {
  const out = goalsToSave(
    [goal(" Launch ", "dan", "Engineering"), goal("  "), goal("Win customers", "Gone", "Sales")],
    ["Rufus", "Dan"],
    ["engineering"],
  );
  assert.deepEqual(out, [
    { title: "Launch", target: "", owner_name: "dan", department_title: "Engineering" },
    { title: "Win customers", target: "", owner_name: "", department_title: "" },
  ]);
});
