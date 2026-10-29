import assert from "node:assert/strict";
import test from "node:test";
import {
  addedByLine,
  cardChanges,
  changeLine,
  contactActivityLines,
  keeperBlocked,
  newlyAdded,
} from "../src/lib/contactChanges.ts";

const change = (extra = {}) => ({
  id: 1,
  person_id: 7,
  action: "add",
  field: "contact",
  email: "priya@northwind.example",
  full_name: "Priya Shah",
  old_value: "",
  new_value: "",
  reason: "emailed",
  evidence: 3,
  status: "applied",
  seen: false,
  created_at: "2026-10-19T12:00:00+00:00",
  decided_at: null,
  ...extra,
});

test("an add says who, where and why", () => {
  assert.deepEqual(changeLine(change()), {
    tag: "Added",
    title: "Priya Shah",
    detail: "priya@northwind.example. You emailed them 3 times lately.",
  });
  assert.equal(changeLine(change({ reason: "met", evidence: 1 })).detail, "priya@northwind.example. Guest at one of your meetings.");
  assert.equal(
    changeLine(change({ reason: "wrote", evidence: 3, status: "suggested" })).detail,
    "priya@northwind.example. They emailed you 3 times lately.",
  );
  assert.equal(changeLine(change({ status: "suggested" })).tag, "Suggested");
});

test("an update says what it is now and where it came from", () => {
  const role = change({ action: "update", field: "role", new_value: "VP Finance, Contoso", reason: "signature" });
  assert.deepEqual(changeLine(role), {
    tag: "Updated",
    title: "Priya Shah",
    detail: "Role is now VP Finance, Contoso. From their email signature.",
  });
  const alias = change({ action: "update", field: "email_alias", new_value: "priya@home.example" });
  assert.equal(changeLine(alias).detail, "Also writes from priya@home.example. You emailed them at this address.");
});

test("the card keeps suggestions and what wasn't cleared", () => {
  const rows = [
    change({ id: 1 }),
    change({ id: 2, seen: true }),
    change({ id: 3, status: "suggested", seen: false, person_id: null }),
    change({ id: 4, status: "undone" }),
  ];
  assert.deepEqual(cardChanges(rows).map((c) => c.id), [1, 3]);
  assert.deepEqual([...newlyAdded(rows)], [7]);
});

test("the switch says what it waits on", () => {
  assert.equal(keeperBlocked("ok"), null);
  assert.equal(keeperBlocked(""), null);
  assert.match(keeperBlocked("act_as_me_off"), /Act as me/);
  assert.match(keeperBlocked("needs_reconnect"), /mailbox/);
});

test("a person's page says how they were added, unless they asked", () => {
  const when = () => "Oct 19";
  assert.equal(addedByLine([change()], when), "Added by me on Oct 19. You emailed them 3 times lately.");
  assert.equal(addedByLine([change({ reason: "asked" })], when), null);
});

const note = (extra = {}) => ({
  person_id: 7,
  note: "",
  topics: [],
  last_emailed_at: null,
  last_heard_at: null,
  last_met_at: null,
  next_meeting_at: null,
  next_meeting_title: "",
  emailed_count: 0,
  met_count: 0,
  note_written_at: null,
  updated_at: "2026-10-19T12:00:00+00:00",
  ...extra,
});
const day = (iso) => iso.slice(0, 10);

test("activity lines say only what is known, next meeting first", () => {
  assert.deepEqual(contactActivityLines(note(), day), []);
  assert.deepEqual(
    contactActivityLines(
      note({
        last_emailed_at: "2026-10-14T09:00:00+00:00",
        emailed_count: 3,
        last_heard_at: "2026-10-15T09:00:00+00:00",
        last_met_at: "2026-10-02T15:00:00+00:00",
        next_meeting_at: "2026-10-23T15:00:00+00:00",
        next_meeting_title: "Roof bid review",
      }),
      day,
    ),
    [
      "Next meeting 2026-10-23: Roof bid review",
      "You last emailed them 2026-10-14 (3 emails in 60 days)",
      "They last wrote to you 2026-10-15",
      "You last met 2026-10-02",
    ],
  );
  assert.deepEqual(contactActivityLines(note({ last_emailed_at: "2026-10-14", emailed_count: 1 }), day), [
    "You last emailed them 2026-10-14",
  ]);
});
