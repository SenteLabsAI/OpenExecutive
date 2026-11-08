import assert from "node:assert/strict";
import test from "node:test";
import { askedBy, draftOf, dueDateValue, groupTasks, shortDue, taskChanges, taskTitle } from "../src/lib/tasks.ts";

const NOW = new Date(2026, 10, 8, 9, 30); // Sun Nov 8, 9:30 local

function task(over = {}) {
  return {
    task_id: 1,
    title: "send the deck",
    description: "Sara committed to: send the deck",
    notes: "",
    status: "open",
    owner_person_id: 2,
    owner_name: "Sara",
    due_at: new Date(2026, 10, 10, 17).toISOString(),
    created_at: new Date(2026, 10, 1).toISOString(),
    updated_at: null,
    completed_at: null,
    closed_reason: null,
    session_id: null,
    ...over,
  };
}

test("groupTasks splits overdue, this week and later, soonest first", () => {
  const groups = groupTasks(
    [
      task({ task_id: 1, due_at: new Date(2026, 10, 20).toISOString() }),
      task({ task_id: 2, due_at: new Date(2026, 10, 7).toISOString() }),
      task({ task_id: 3, due_at: new Date(2026, 10, 12).toISOString() }),
      task({ task_id: 4, due_at: new Date(2026, 10, 9).toISOString() }),
    ],
    NOW,
  );
  assert.deepEqual(
    groups.map((g) => [g.key, g.tasks.map((t) => t.task_id)]),
    [
      ["overdue", [2]],
      ["week", [4, 3]],
      ["later", [1]],
    ],
  );
});

test("groupTasks leaves out empty groups", () => {
  assert.deepEqual(groupTasks([task()], NOW).map((g) => g.label), ["This week"]);
  assert.deepEqual(groupTasks([], NOW), []);
});

test("shortDue reads today, tomorrow, the weekday within a week, else the date", () => {
  assert.equal(shortDue(new Date(2026, 10, 8, 18).toISOString(), NOW), "Today");
  assert.equal(shortDue(new Date(2026, 10, 9, 8).toISOString(), NOW), "Tomorrow");
  assert.equal(shortDue(new Date(2026, 10, 12).toISOString(), NOW), "Thu, Nov 12");
  assert.equal(shortDue(new Date(2026, 11, 1).toISOString(), NOW), "Dec 1");
  assert.equal(shortDue(new Date(2026, 10, 6).toISOString(), NOW), "Nov 6");
  assert.equal(shortDue(null, NOW), "");
});

test("dueDateValue is the local date the date input holds", () => {
  assert.equal(dueDateValue(new Date(2026, 0, 5, 23).toISOString()), "2026-01-05");
  assert.equal(dueDateValue(null), "");
});

test("taskTitle capitalizes; askedBy names who asked when it isn't the owner", () => {
  assert.equal(taskTitle({ title: " send the deck" }), "Send the deck");
  assert.equal(askedBy({ description: "Pat asked Sara for: the numbers", owner_name: "Sara" }), "Pat");
  assert.equal(askedBy({ description: "Sara committed to: the numbers", owner_name: "Sara" }), null);
});

test("taskChanges sends only what changed, with what the editor saw", () => {
  const t = task({ notes: "two quotes" });
  const draft = { ...draftOf(t), title: "Send the final deck", status: "waiting", owner_person_id: 3 };
  assert.deepEqual(taskChanges(t, draft), {
    changes: { title: "Send the final deck", status: "waiting", owner_person_id: 3 },
    base: { title: "send the deck", status: "open", owner_person_id: 2 },
  });
  assert.deepEqual(taskChanges(t, draftOf(t)), { changes: {}, base: {} });
  const moved = taskChanges(t, { ...draftOf(t), due_date: "2026-11-20" });
  assert.deepEqual(moved, { changes: { due_date: "2026-11-20" }, base: { due_date: "2026-11-10" } });
});
