// Pure helpers behind the Tasks page (app/tasks/page.tsx): grouping, the
// short due and source lines, and what an edit changed. No React here so
// `npm test` can exercise it (scripts/tasks.test.mjs).
import type { Task, TaskBase, TaskChanges } from "./api";

const DAY_MS = 24 * 60 * 60 * 1000;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const TASK_STATUS_LABEL: Record<string, string> = {
  open: "Open",
  in_progress: "In progress",
  waiting: "Waiting",
  done: "Done",
};

export interface TaskGroup {
  key: "overdue" | "week" | "later";
  label: string;
  tasks: Task[];
}

function dueTime(t: Task): number {
  const ms = t.due_at ? Date.parse(t.due_at) : NaN;
  return Number.isFinite(ms) ? ms : Infinity;
}

/** Open tasks as Overdue, This week (due within seven days) and Later,
 * soonest first; empty groups left out. */
export function groupTasks(tasks: readonly Task[], now: Date): TaskGroup[] {
  const groups: TaskGroup[] = [
    { key: "overdue", label: "Overdue", tasks: [] },
    { key: "week", label: "This week", tasks: [] },
    { key: "later", label: "Later", tasks: [] },
  ];
  const sorted = [...tasks].sort((a, b) => dueTime(a) - dueTime(b));
  for (const t of sorted) {
    const due = dueTime(t);
    const g = due <= now.getTime() ? 0 : due - now.getTime() <= 7 * DAY_MS ? 1 : 2;
    groups[g].tasks.push(t);
  }
  return groups.filter((g) => g.tasks.length > 0);
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "Today", "Tomorrow", "Thu, Nov 13" within a week, else "Dec 1". */
export function shortDue(iso: string | null, now: Date): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.round((startOfDay(d) - startOfDay(now)) / DAY_MS);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  const date = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return days > 1 && days < 7 ? `${WEEKDAYS[d.getDay()]}, ${date}` : date;
}

/** The due date as the date input holds it (YYYY-MM-DD, local). */
export function dueDateValue(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

/** The title, first letter up. */
export function taskTitle(t: Pick<Task, "title">): string {
  const s = t.title.trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** Who asked, when it is someone other than its owner: "Pat asked Sara for: …"
 * → "Pat asked". Null for one the owner took on themselves. */
export function askedBy(t: Pick<Task, "description" | "owner_name">): string | null {
  const m = t.description.match(/^(.+?) asked (.+?) for: /);
  return m && m[1] !== t.owner_name ? m[1] : null;
}

export interface TaskDraft {
  title: string;
  notes: string;
  status: string;
  due_date: string;
  owner_person_id: number;
}

export function draftOf(t: Task): TaskDraft {
  return {
    title: t.title,
    notes: t.notes,
    status: t.status,
    due_date: dueDateValue(t.due_at),
    owner_person_id: t.owner_person_id,
  };
}

/** What the edit changed, and what the editor saw for each of those fields
 * (sent so the server can tell when someone else changed them meanwhile). */
export function taskChanges(t: Task, draft: TaskDraft): { changes: TaskChanges; base: TaskBase } {
  const seen = draftOf(t);
  const changes: TaskChanges = {};
  const base: TaskBase = {};
  if (draft.title.trim() !== seen.title.trim()) {
    changes.title = draft.title.trim();
    base.title = t.title;
  }
  if (draft.notes.trim() !== seen.notes.trim()) {
    changes.notes = draft.notes.trim();
    base.notes = t.notes;
  }
  if (draft.status !== seen.status && draft.status !== "done") {
    changes.status = draft.status as TaskChanges["status"];
    base.status = t.status;
  }
  if (draft.due_date && draft.due_date !== seen.due_date) {
    changes.due_date = draft.due_date;
    base.due_date = seen.due_date || null;
  }
  if (draft.owner_person_id !== seen.owner_person_id) {
    changes.owner_person_id = draft.owner_person_id;
    base.owner_person_id = t.owner_person_id;
  }
  return { changes, base };
}
