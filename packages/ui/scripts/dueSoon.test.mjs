import assert from "node:assert/strict";
import test from "node:test";
import { dueLabel, dueSoon, loopText, principalIdOf } from "../src/lib/dueSoon.ts";

// Local-time constructors, so the labels hold in any test-runner time zone.
const NOW = new Date(2026, 8, 25, 10, 0); // Fri Sep 25 2026, 10:00 local
const at = (day, hour = 17) => new Date(2026, 8, day, hour, 0);

const loop = (id, due, description = "Pat Lee committed to: send the proposal") => ({
  loop_id: id,
  owner_person_id: 1,
  owner_name: "Pat Lee",
  description,
  due_at: due.toISOString(),
  created_at: NOW.toISOString(),
});

test("principalIdOf picks the oldest principal, or null", () => {
  assert.equal(principalIdOf([]), null);
  assert.equal(principalIdOf([{ id: 4, is_principal: false }]), null);
  assert.equal(
    principalIdOf([
      { id: 9, is_principal: true },
      { id: 2, is_principal: false },
      { id: 5, is_principal: true },
    ]),
    5,
  );
});

test("loopText addresses the owner", () => {
  assert.equal(loopText(loop(1, NOW)), "Send the proposal");
  assert.equal(
    loopText(loop(1, NOW, "Sam Ortiz asked Pat Lee for: the signed contract")),
    "Sam Ortiz asked you for: the signed contract",
  );
  // Anything not in the stored shapes is shown as stored.
  assert.equal(loopText(loop(1, NOW, "Renew the domain")), "Renew the domain");
  assert.equal(
    loopText({ description: "Pat Lee committed to: ", owner_name: "Pat Lee" }),
    "Pat Lee committed to: ",
  );
});

test("dueLabel reads overdue, today, tomorrow and a weekday", () => {
  assert.equal(dueLabel(at(23), NOW), "Overdue since Sep 23");
  assert.equal(dueLabel(at(25), NOW), "Due today");
  assert.equal(dueLabel(at(26), NOW), "Due tomorrow");
  assert.equal(dueLabel(at(28), NOW), "Due Mon, Sep 28");
  // Earlier today counts as overdue.
  assert.equal(dueLabel(at(25, 9), NOW), "Overdue since Sep 25");
});

test("dueSoon lists overdue and this week, soonest first, and counts the rest", () => {
  const view = dueSoon([loop(3, at(30)), loop(1, at(23)), loop(2, at(26)), loop(4, new Date(2026, 9, 9))], NOW);
  assert.deepEqual(
    view.items.map((i) => [i.loop.loop_id, i.overdue, i.dueLabel]),
    [
      [1, true, "Overdue since Sep 23"],
      [2, false, "Due tomorrow"],
      [3, false, "Due Wed, Sep 30"],
    ],
  );
  assert.equal(view.later, 1);
  assert.equal(view.items[0].text, "Send the proposal");
});

test("dueSoon skips an unreadable due time and handles nothing open", () => {
  const bad = { ...loop(7, NOW), due_at: "not a date" };
  assert.deepEqual(dueSoon([bad], NOW), { items: [], later: 0 });
  assert.deepEqual(dueSoon([], NOW), { items: [], later: 0 });
});
