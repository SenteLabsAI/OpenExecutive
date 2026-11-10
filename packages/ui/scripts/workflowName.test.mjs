import assert from "node:assert/strict";
import test from "node:test";
import { newStepId, workflowNameFromTitle } from "../src/lib/workflowChanges.ts";

const none = new Set();

test("the name comes from the title", () => {
  assert.equal(workflowNameFromTitle("Weekly Competitor Watch", none), "weekly_competitor_watch");
  assert.equal(workflowNameFromTitle("  Cash & runway — Monday!  ", none), "cash_runway_monday");
  assert.equal(workflowNameFromTitle("2026 plan", none), "workflow_2026_plan");
  assert.equal(workflowNameFromTitle("", none), "workflow");
  assert.equal(workflowNameFromTitle("Go", none), "go_workflow");
  assert.equal(workflowNameFromTitle("Quick check", none), "my_quick_check");
});

test("the name fits the server's rule and is unique", () => {
  const long = workflowNameFromTitle("a ".repeat(60), none);
  assert.match(long, /^[a-z][a-z0-9_]{2,48}$/);
  const taken = new Set(["weekly_review", "weekly_review_2"]);
  assert.equal(workflowNameFromTitle("Weekly review", taken), "weekly_review_3");
  const longTaken = new Set([workflowNameFromTitle("x".repeat(80), none)]);
  const next = workflowNameFromTitle("x".repeat(80), longTaken);
  assert.match(next, /^[a-z][a-z0-9_]{2,48}$/);
  assert.ok(next.endsWith("_2"));
});

test("a new step gets an id no other step has", () => {
  assert.equal(newStepId([]), "step_1");
  assert.equal(newStepId(["step_1", "step_3"]), "step_2");
});
