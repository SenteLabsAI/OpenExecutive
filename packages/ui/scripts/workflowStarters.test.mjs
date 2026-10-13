import assert from "node:assert/strict";
import test from "node:test";
import {
  SOLO_STARTER_PICKS,
  WORKFLOW_STARTERS,
  WORKFLOW_STARTER_CHIPS,
  starterPicksFor,
  workflowStarterChipsFor,
  workflowStartersFor,
} from "../src/lib/workflowStarters.ts";

test("a team workspace keeps today's picks and chips", () => {
  assert.deepEqual(
    starterPicksFor("team").map((p) => p.name),
    ["investor_update", "board_prep", "mbr", "competitive_teardown"],
  );
  assert.equal(workflowStarterChipsFor("team"), WORKFLOW_STARTER_CHIPS);
  assert.equal(workflowStarterChipsFor("team")[1].label, "Board pre-read");
  assert.equal(workflowStartersFor("team"), WORKFLOW_STARTERS);
});

test("a solo workspace gets the owner-operator picks", () => {
  assert.deepEqual(
    starterPicksFor("solo").map((p) => p.name),
    ["mbr", "pricing_review", "churn_deep_dive", "quarterly_plan"],
  );
  assert.match(starterPicksFor("solo")[0].useFor, /^Monthly review: last month against plan/);
  for (const p of SOLO_STARTER_PICKS) {
    assert.ok(p.useFor.length > 0 && p.useFor.length <= 90, p.name);
  }
});

test("solo chips: cash and pipeline, lost customers, competitor digest", () => {
  const texts = workflowStarterChipsFor("solo").map((c) => c.text);
  assert.deepEqual(texts, [
    "A Friday cash and pipeline check sent to me",
    "A monthly look at which customers stopped buying",
    "A weekly competitor digest sent to me every Monday morning",
  ]);
  assert.deepEqual(workflowStartersFor("solo"), texts);
  // Nothing that assumes a board, investors or a team to sign off.
  const all = [...texts, ...SOLO_STARTER_PICKS.map((p) => p.useFor)].join(" ");
  assert.doesNotMatch(all, /board|investor|CFO|hiring/i);
});
