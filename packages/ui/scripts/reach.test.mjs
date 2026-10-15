import assert from "node:assert/strict";
import test from "node:test";
import { REACH_HREF, reachOf, reachSummary, sentTodayLine, stepLabel } from "../src/lib/reach.ts";

const settings = (over = {}) => ({
  enabled: true,
  inbox: { enabled: true },
  handle_it: { enabled: true, mode: "balanced", available: true, sent_today: 0, lead: false },
  training: { replies: false, follow_ups: false, drafts: false },
  ...over,
});

test("reachOf reads the dial step from the settings", () => {
  assert.equal(reachOf(settings({ enabled: false })), "off");
  assert.equal(reachOf(settings({ inbox: { enabled: false } })), "when_asked");
  assert.equal(reachOf(settings({ handle_it: { enabled: false, mode: "balanced" } })), "inbox");
  assert.equal(reachOf(settings()), "balanced");
  assert.equal(reachOf(settings({ handle_it: { enabled: true, mode: "bold", lead: true } })), "everything");
});

test("stepLabel names every step as the dial does", () => {
  assert.equal(stepLabel("off"), "Off");
  assert.equal(stepLabel("inbox"), "Drafts for my inbox");
  assert.equal(stepLabel("careful"), "Easy ones");
  assert.equal(stepLabel("balanced"), "People I know");
  assert.equal(stepLabel("bold"), "Most mail");
  assert.equal(stepLabel("everything"), "Everything");
});

test("reachSummary says when Handle it for me is off", () => {
  const s = reachSummary(settings({ handle_it: { enabled: false, mode: "balanced" } }));
  assert.equal(s.label, "Drafts for my inbox");
  assert.equal(s.sends, false);
  assert.equal(s.training, false);
  assert.match(s.line, /off/);
});

test("reachSummary marks a sending step in training", () => {
  const s = reachSummary(settings({ training: { replies: true, follow_ups: true } }));
  assert.equal(s.label, "People I know");
  assert.equal(s.sends, true);
  assert.equal(s.training, true);
});

test("reachSummary never marks Everything in training", () => {
  const s = reachSummary(
    settings({ handle_it: { enabled: true, mode: "bold", lead: true }, training: { replies: true } }),
  );
  assert.equal(s.label, "Everything");
  assert.equal(s.training, false);
});

test("the dial link points at Act as me", () => {
  assert.equal(REACH_HREF, "/delegate/act-as-me#how-far");
  assert.equal(sentTodayLine(1), "Sent 1 reply on its own today.");
  assert.equal(sentTodayLine(3), "Sent 3 replies on its own today.");
});
