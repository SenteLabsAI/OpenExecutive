import assert from "node:assert/strict";
import test from "node:test";
import {
  ADVANCED_VIEWS,
  isAdvancedView,
  viewForSelection,
  viewFromParam,
} from "../src/lib/knowledgeViews.ts";

test("company documents is the only view up front", () => {
  assert.equal(isAdvancedView("company"), false);
  for (const v of ADVANCED_VIEWS) assert.equal(isAdvancedView(v), true);
});

test("?view=review opens the review queue; unknown values open documents", () => {
  assert.equal(viewFromParam("review"), "review");
  assert.equal(viewFromParam("playbooks"), "playbooks");
  assert.equal(viewFromParam(null), "company");
  assert.equal(viewFromParam("company"), "company");
  assert.equal(viewFromParam("nope"), "company");
});

test("a built-in file and the new-file form sit under the playbooks view", () => {
  assert.equal(viewForSelection("file"), "playbooks");
  assert.equal(viewForSelection("new"), "playbooks");
  assert.equal(viewForSelection("playbooks"), "playbooks");
  assert.equal(viewForSelection("query"), "query");
  assert.equal(viewForSelection(null), "company");
});
