import assert from "node:assert/strict";
import test from "node:test";
import {
  defaultKindForTab,
  defaultPeopleTab,
  effectiveKind,
  hiddenTeamCount,
  isContact,
  personCardStatus,
  peopleForTab,
  shouldOfferTeamMode,
  tabsFor,
} from "../src/lib/peopleKinds.ts";

const person = (id, kind, is_principal = false) => ({ id, kind, is_principal });

const PRINCIPAL = person(1, "team", true);
const TEAMMATE = person(2, "team");
const CLIENT = person(3, "contact");
const LEGACY = { id: 4, is_principal: false }; // an older server sends no kind
const ROSTER = [PRINCIPAL, TEAMMATE, CLIENT, LEGACY];

test("a row with no kind is a team member, not a contact", () => {
  assert.equal(isContact(LEGACY), false);
  assert.equal(isContact(CLIENT), true);
});

test("solo opens on Contacts, team on Team", () => {
  assert.equal(defaultPeopleTab("solo"), "contacts");
  assert.equal(defaultPeopleTab("team"), "team");
});

test("contacts are the principal's alone: nobody else gets the tab", () => {
  assert.deepEqual(tabsFor(true), ["team", "contacts"]);
  assert.deepEqual(tabsFor(false), ["team"]);
  assert.equal(defaultPeopleTab("solo", false), "team");
  assert.equal(defaultPeopleTab("team", false), "team");
});

test("the Team tab lists the whole team in team mode", () => {
  assert.deepEqual(peopleForTab(ROSTER, "team", "team").map((p) => p.id), [1, 2, 4]);
});

test("the Team tab is just the principal in solo mode", () => {
  assert.deepEqual(peopleForTab(ROSTER, "team", "solo").map((p) => p.id), [1]);
  assert.equal(hiddenTeamCount(ROSTER, "solo"), 2);
  assert.equal(hiddenTeamCount(ROSTER, "team"), 0);
});

test("the Contacts tab lists only contacts, in either mode", () => {
  assert.deepEqual(peopleForTab(ROSTER, "contacts", "team").map((p) => p.id), [3]);
  assert.deepEqual(peopleForTab(ROSTER, "contacts", "solo").map((p) => p.id), [3]);
});

test("the add form starts on the open tab's kind", () => {
  assert.equal(defaultKindForTab("contacts"), "contact");
  assert.equal(defaultKindForTab("team"), "team");
});

test("the principal is always on the team", () => {
  assert.equal(effectiveKind("contact", true), "team");
  assert.equal(effectiveKind("contact", false), "contact");
});

test("adding a teammate in solo offers team mode; a contact or the principal does not", () => {
  assert.equal(shouldOfferTeamMode("solo", "team", false), true);
  assert.equal(shouldOfferTeamMode("solo", "contact", false), false);
  assert.equal(shouldOfferTeamMode("solo", "team", true), false);
  assert.equal(shouldOfferTeamMode("solo", "contact", true), false);
  assert.equal(shouldOfferTeamMode("team", "team", false), false);
});

test("a card's one status: email for a contact, leave or reply time for the team", () => {
  const base = { kind: "team", email: null, on_leave_until: null, response_sla_hours: 12 };
  assert.deepEqual(personCardStatus(base, "2026-10-03"), { label: "Replies within 12h", tone: "ok" });
  assert.deepEqual(
    personCardStatus({ ...base, on_leave_until: "2026-10-10" }, "2026-10-03"),
    { label: "On leave until 2026-10-10", tone: "warn" },
  );
  // A leave that has ended no longer shows.
  assert.equal(personCardStatus({ ...base, on_leave_until: "2026-09-30" }, "2026-10-03").label, "Replies within 12h");
  assert.deepEqual(personCardStatus({ ...base, kind: "contact" }, "2026-10-03"), { label: "No email", tone: "muted" });
  assert.equal(personCardStatus({ ...base, kind: "contact", email: "a@b.c" }, "2026-10-03").label, "Email on file");
});
