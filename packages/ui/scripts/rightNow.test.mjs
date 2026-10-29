import assert from "node:assert/strict";
import { test } from "node:test";

import {
  alsoInLine,
  checkedLabel,
  clockLabel,
  isNewSince,
  newSinceLabel,
  pointTarget,
  progressNote,
  progressSteps,
  seenBaseline,
} from "../src/lib/rightNow.ts";

const at = (h, m) => {
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d;
};

test("clockLabel formats a 12-hour time", () => {
  assert.equal(clockLabel(at(14, 5).toISOString()), "2:05 PM");
  assert.equal(clockLabel(at(0, 30).toISOString()), "12:30 AM");
  assert.equal(clockLabel(null), null);
  assert.equal(clockLabel("nope"), null);
});

test("only a look earlier today is a baseline", () => {
  const now = at(15, 0);
  assert.equal(seenBaseline(at(11, 5).toISOString(), now)?.getTime(), at(11, 5).getTime());
  const yesterday = new Date(now.getTime() - 86_400_000);
  assert.equal(seenBaseline(yesterday.toISOString(), now), null);
  assert.equal(seenBaseline(null, now), null);
});

test("points after the baseline are new", () => {
  const baseline = at(11, 5);
  const points = [
    { text: "a", since: at(14, 31).toISOString() },
    { text: "b", since: at(13, 50).toISOString() },
    { text: "c", since: at(8, 0).toISOString() },
    { text: "d", since: null },
  ];
  assert.deepEqual(points.map((p) => isNewSince(p, baseline)), [true, true, false, false]);
  assert.equal(newSinceLabel(points, baseline), "2 new since 11:05 AM");
  assert.equal(newSinceLabel(points, null), null);
  assert.equal(newSinceLabel(points.slice(2), baseline), null);
});

test("checkedLabel says when the header was last found current", () => {
  const at = new Date(2026, 9, 26, 14, 20).toISOString();
  assert.equal(checkedLabel(at), "Checked 2:20 PM");
  assert.equal(checkedLabel(null), null);
  assert.equal(checkedLabel("nope"), null);
});

test("alsoInLine names a few and counts the rest", () => {
  assert.equal(alsoInLine([], 0), null);
  assert.equal(alsoInLine(undefined, undefined), null);
  assert.equal(alsoInLine(["Yardi: Webinar"], 1), "Also in: 1 other — Yardi: Webinar.");
  assert.equal(
    alsoInLine(["Ana: Rates", "Yardi: Webinar"], 2),
    "Also in: 2 others — Ana: Rates and Yardi: Webinar.",
  );
  assert.equal(
    alsoInLine(["Ana: Rates", "Yardi: Webinar", "Bo: Hi"], 5),
    "Also in: 5 others — Ana: Rates, Yardi: Webinar, Bo: Hi and 2 more.",
  );
  assert.equal(alsoInLine([], 3), "Also in: 3 others.");
});

test("a point opens the email or card it is about", () => {
  assert.deepEqual(
    pointTarget({ text: "a", source: { kind: "mail", message_id: "m1", thread_id: "t1", decision_id: 4 } }),
    { kind: "mail", threadId: "t1", messageId: "m1", cardId: 4 },
  );
  assert.deepEqual(pointTarget({ text: "a", source: { kind: "alert", alert_id: 9 } }), { kind: "alert", alertId: 9 });
  // An email without its conversation, or no source, opens nothing.
  assert.equal(pointTarget({ text: "a", source: { kind: "mail", message_id: "m1" } }), null);
  assert.equal(pointTarget({ text: "a" }), null);
  assert.deepEqual(
    pointTarget({ text: "a", source: { kind: "link", url: "https://acme.slack.com/archives/C1/p1", app: "Slack" } }),
    { kind: "link", url: "https://acme.slack.com/archives/C1/p1", app: "Slack" },
  );
  // Only an https link opens.
  assert.equal(pointTarget({ text: "a", source: { kind: "link", url: "javascript:alert(1)", app: "Slack" } }), null);
});

test("progress steps follow the step the server reports", () => {
  assert.deepEqual(
    progressSteps("writing").map((s) => s.state),
    ["done", "now", "todo"],
  );
  assert.equal(progressSteps("writing")[0].label, "Checked your inbox, calendar and Needs you");
  assert.equal(progressSteps("checking")[1].label, "Wrote what matters now");
  // Not started yet, or a step this page doesn't know: the first is running.
  assert.deepEqual(progressSteps(null).map((s) => s.state), ["now", "todo", "todo"]);
  assert.deepEqual(progressSteps("later").map((s) => s.state), ["now", "todo", "todo"]);
});

test("the progress note counts new email since the last header", () => {
  const since = at(15, 12).toISOString();
  assert.equal(progressNote(2, since), "2 new emails since 3:12 PM");
  assert.equal(progressNote(1, since), "1 new email since 3:12 PM");
  assert.equal(progressNote(0, since), null);
  assert.equal(progressNote(3, null), null);
});
