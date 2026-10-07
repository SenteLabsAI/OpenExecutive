import assert from "node:assert/strict";
import test from "node:test";
import { addedTools, describeCadence, describeChanges } from "../src/lib/workflowChanges.ts";

const people = { 1: "Sarah", 2: "Mark" };
const labels = {
  person: (id) => people[id] ?? `person #${id}`,
  specialist: (k) => ({ cso: "Strategy", cfo: "Finance" })[k] ?? k,
  tool: (n) => n.replace(/^.*__/, "").replace(/_/g, " "),
};

function base() {
  return {
    name: "weekly_digest",
    title: "Weekly digest",
    description: "What competitors shipped.",
    section: "Growth & GTM",
    estimated_minutes: 5,
    input_fields: [{ name: "competitors", label: "Competitors", required: true, multiline: false }],
    steps: [
      { kind: "specialist", id: "scan", title: "Scan", specialist: "cso", goal: "Summarize {competitors}." },
      { kind: "approval_gate", id: "ok", title: "Sign-off", person_id: 1, question: "Send it?" },
      { kind: "action", id: "send", title: "Send", goal: "Email it.", tools: ["gw__send_gmail_message"] },
      { kind: "synthesis", id: "assemble", title: "Assemble", instructions: "", specialist: "cso" },
    ],
    cadence: "weekly@mon@09:00",
    cadence_person_id: 1,
    is_active: true,
  };
}

const clone = (o) => JSON.parse(JSON.stringify(o));

test("no changes reads as an empty list", () => {
  assert.deepEqual(describeChanges(base(), base(), labels), []);
});

test("server-owned fields are not changes", () => {
  const after = { ...base(), is_active: false, updated_at: "2026-01-01", estimated_minutes: 9 };
  assert.deepEqual(describeChanges(base(), after, labels), []);
});

test("sign-off person and schedule are named in plain words", () => {
  const after = clone(base());
  after.steps[1].person_id = 2;
  after.cadence = "weekly@fri@09:00";
  assert.deepEqual(describeChanges(base(), after, labels), [
    "Schedule: Every Friday at 09:00 UTC, sent to Sarah (was: Every Monday at 09:00 UTC)",
    "“Sign-off”: sign-off from Mark instead of Sarah",
  ]);
});

test("turning the schedule off, and changing only the recipient", () => {
  const off = { ...base(), cadence: null, cadence_person_id: null };
  assert.deepEqual(describeChanges(base(), off, labels), [
    "Schedule: only when you run it (was: Every Monday at 09:00 UTC)",
  ]);
  const recipient = { ...base(), cadence_person_id: 2 };
  assert.deepEqual(describeChanges(base(), recipient, labels), [
    "Scheduled results go to Mark instead of Sarah",
  ]);
});

test("specialist, tools and instructions on a step", () => {
  const after = clone(base());
  after.steps[0].specialist = "cfo";
  after.steps[0].goal = "Summarize {competitors} pricing.";
  after.steps[2].tools = ["gw__create_doc"];
  assert.deepEqual(describeChanges(base(), after, labels), [
    "“Scan” is now handled by Finance instead of Strategy",
    "New instructions for “Scan”",
    "“Send” can now use create doc",
    "“Send” no longer uses send gmail message",
  ]);
});

test("added, removed and reordered steps", () => {
  const after = clone(base());
  after.steps = [
    after.steps[2],
    after.steps[0],
    { kind: "specialist", id: "price", title: "Pricing", specialist: "cfo", goal: "Check pricing." },
    after.steps[3],
  ];
  assert.deepEqual(describeChanges(base(), after, labels), [
    "Added step 3: “Pricing” (Finance)",
    "Removed step “Sign-off”",
    "Changed the order of the steps",
  ]);
});

test("an added tool step says how many tools it uses", () => {
  const after = clone(base());
  after.steps.splice(1, 0, { kind: "action", id: "log", title: "Log it", goal: "Log.", tools: ["gw__append_rows"] });
  assert.deepEqual(describeChanges(base(), after, labels), ["Added step 2: “Log it” (uses 1 tool)"]);
});

test("a re-numbered step is matched by its title", () => {
  const after = clone(base());
  after.steps[0].id = "step_1";
  assert.deepEqual(describeChanges(base(), after, labels), []);
});

test("inputs added, removed, renamed and made optional", () => {
  const after = clone(base());
  after.input_fields = [
    { name: "competitors", label: "Rivals", required: false, multiline: false },
    { name: "region", label: "Region", required: true, multiline: false },
  ];
  assert.deepEqual(describeChanges(base(), after, labels), [
    "Input “Competitors” is now called “Rivals”",
    "“Rivals” is now optional",
    "Asks for “Region” on each run",
  ]);
});

test("title, description and section", () => {
  const after = { ...base(), title: "Friday digest", description: "New.", section: "Product" };
  assert.deepEqual(describeChanges(base(), after, labels), [
    "Renamed “Weekly digest” to “Friday digest”",
    "Updated the description",
    "Moved from Growth & GTM to Product",
  ]);
});

test("addedTools lists only tools the saved version lacked", () => {
  const after = clone(base());
  after.steps[2].tools = ["gw__send_gmail_message", "gw__create_doc"];
  assert.deepEqual(addedTools(base(), after), ["gw__create_doc"]);
  assert.deepEqual(addedTools(base(), base()), []);
});

test("describeCadence", () => {
  assert.equal(describeCadence(null), "Only when you run it");
  assert.equal(describeCadence("daily@07:30"), "Every day at 07:30 UTC");
  assert.equal(describeCadence("quarterly@05-10:00"), "Quarterly on day 5 at 10:00 UTC");
});
