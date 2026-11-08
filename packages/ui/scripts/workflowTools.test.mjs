import assert from "node:assert/strict";
import test from "node:test";
import { groupTools, stepApps, toolAction, toolApp } from "../src/lib/workflowTools.ts";
import { describeCadenceLocal } from "../src/lib/workflowChanges.ts";

test("tools are named by the app they work in", () => {
  assert.deepEqual(toolApp("oe__browser_click"), { label: "Browser", kind: "browser" });
  assert.equal(toolApp("google_workspace__send_gmail_message").label, "Gmail");
  assert.equal(toolApp("ms365__send_mail").label, "Outlook");
  assert.equal(toolApp("oe__send_email_as_me").label, "Email");
  assert.equal(toolApp("google_workspace__get_events").label, "Google Calendar");
  assert.equal(toolApp("google_workspace__append_table_rows").kind, "other");
  assert.equal(toolApp("google_workspace__read_sheet_values").label, "Google Drive");
  assert.equal(toolApp("oe__message_person").label, "Messages");
  assert.equal(toolApp("oe__create_alert").label, "Open Executive");
  assert.equal(toolApp("hubspot__create_contact").label, "Hubspot");
  assert.equal(toolApp("hubspot__create_email_campaign").label, "Hubspot");
  assert.equal(toolApp("datadog__list_events").label, "Datadog");
  assert.equal(toolApp("slack__post_message").label, "Slack");
  assert.equal(toolApp("hubspot__post_to_slack_channel").label, "Hubspot");
});

test("browser tools say what they do to a page", () => {
  assert.equal(toolAction("oe__browser_navigate"), "open pages");
  assert.equal(toolAction("oe__browser_look"), "read pages");
  assert.equal(toolAction("oe__browser_type"), "type");
  assert.equal(toolAction("google_workspace__send_gmail_message"), "send gmail message");
});

test("tools group by app, split into changes and look-only, once each", () => {
  const readOnly = (n) => /look|scroll/.test(n);
  const groups = groupTools(
    [
      "oe__browser_navigate",
      "oe__browser_look",
      "oe__browser_click",
      "oe__browser_scroll",
      "oe__browser_navigate",
      "oe__message_person",
    ],
    readOnly,
  );
  assert.deepEqual(
    groups.map((g) => [g.app.label, g.changes.map((a) => a.action), g.looks.map((a) => a.action)]),
    [
      ["Browser", ["open pages", "click"], ["read pages", "scroll"]],
      ["Messages", ["message person"], []],
    ],
  );
});

test("a step's apps are listed once, in order", () => {
  assert.deepEqual(
    stepApps(["oe__browser_look", "oe__browser_click", "oe__message_person"]).map((a) => a.label),
    ["Browser", "Messages"],
  );
  assert.deepEqual(stepApps([]), []);
});

test("schedules show in the viewer's own time, with summer time", () => {
  const winter = new Date(Date.UTC(2026, 10, 8, 12));
  const summer = new Date(Date.UTC(2026, 6, 8, 12));
  assert.equal(describeCadenceLocal("weekly@mon@14:00", "America/New_York", winter), "Mondays at 9:00 AM EST");
  assert.equal(describeCadenceLocal("weekly@mon@14:00", "America/New_York", summer), "Mondays at 10:00 AM EDT");
  assert.equal(describeCadenceLocal("daily@02:00", "America/New_York", winter), "Every day at 9:00 PM EST");
  assert.equal(describeCadenceLocal("weekly@mon@02:00", "America/New_York", winter), "Sundays at 9:00 PM EST");
  assert.equal(describeCadenceLocal(null), "Only when you run it");
  assert.equal(describeCadenceLocal("odd"), "odd");
});

test("an impossible time falls back to the saved text instead of rolling over", () => {
  assert.equal(describeCadenceLocal("daily@25:00", "America/New_York"), "Every day at 25:00 UTC");
  assert.equal(describeCadenceLocal("weekly@mon@09:60", "America/New_York"), "Every Monday at 09:60 UTC");
});

test("a quarterly run names the local day", () => {
  const now = new Date(Date.UTC(2026, 10, 8, 12));
  assert.equal(
    describeCadenceLocal("quarterly@01-02:00", "America/New_York", now),
    "Quarterly on day 31 at 9:00 PM EST",
  );
  assert.equal(
    describeCadenceLocal("quarterly@15-14:00", "America/New_York", now),
    "Quarterly on day 15 at 9:00 AM EST",
  );
  // Oct 20: Oct 15 has passed, so the next run is Jan 15, in winter time.
  const oct = new Date(Date.UTC(2026, 9, 20, 12));
  assert.equal(
    describeCadenceLocal("quarterly@15-14:00", "America/New_York", oct),
    "Quarterly on day 15 at 9:00 AM EST",
  );
});

test("a schedule change can be said in local time", async () => {
  const { describeChanges } = await import("../src/lib/workflowChanges.ts");
  const before = { name: "w", title: "W", description: "", section: "S", estimated_minutes: 5, input_fields: [], steps: [], cadence: "weekly@mon@02:00", cadence_person_id: 1 };
  const after = { ...before, cadence: "daily@02:00" };
  const labels = { person: () => "Sam", specialist: (k) => k, tool: (t) => t };
  const local = (c) => describeCadenceLocal(c, "America/New_York", new Date(Date.UTC(2026, 10, 8, 12)));
  assert.deepEqual(describeChanges(before, after, { ...labels, cadence: local }), [
    "Schedule: Every day at 9:00 PM EST, sent to Sam (was: Sundays at 9:00 PM EST)",
  ]);
  assert.deepEqual(describeChanges(before, after, labels), [
    "Schedule: Every day at 02:00 UTC, sent to Sam (was: Every Monday at 02:00 UTC)",
  ]);
});
