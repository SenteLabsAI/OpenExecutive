"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import Button from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import { PageSpinner } from "@/components/ui/Spinner";
import {
  completeTask,
  createTask,
  getPeopleViewer,
  listPeople,
  listTasks,
  reopenTask,
  TaskConflict,
  updateTask,
  type PeopleViewer,
  type Person,
  type Task,
} from "@/lib/api";
import {
  askedBy,
  draftOf,
  groupTasks,
  shortDue,
  TASK_STATUS_LABEL,
  taskChanges,
  taskTitle,
  type TaskDraft,
} from "@/lib/tasks";

// Every task in one place (issue 621): what you and your team owe, grouped
// by when it is due, each editable in place. The same rows the person page
// lists and the Executive follows up on.

const PAGE = 100;

const fieldCls =
  "w-full px-3 py-2 text-[15px] rounded-xl bg-surface border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40";
const selectCls =
  "px-3 py-2 text-sm rounded-xl bg-surface border border-line text-fg focus:outline-none focus:ring-2 focus:ring-accent/40";
const segCls = (on: boolean) =>
  `px-3.5 py-1.5 text-sm rounded-lg transition-colors ${
    on ? "bg-surface-overlay text-fg font-medium shadow-sm" : "text-fg-muted hover:text-fg"
  }`;

const STATUS_BADGE: Record<string, string> = {
  in_progress: "bg-accent/15 text-accent",
  waiting: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
};

function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex gap-1 p-1 rounded-xl bg-surface border border-line">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          className={segCls(value === o.value)}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function ownerLabel(t: Task, viewer: PeopleViewer | null): string {
  return viewer?.person_id === t.owner_person_id ? "You" : t.owner_name;
}

function SourceLine({ task, viewer, viewerName }: { task: Task; viewer: PeopleViewer | null; viewerName: string | null }) {
  const asker = askedBy(task);
  const parts: ReactNode[] = [];
  if (task.session_id) {
    parts.push(
      <Link
        key="chat"
        href={`/?session=${encodeURIComponent(task.session_id)}`}
        className="text-accent hover:underline"
      >
        From a chat
      </Link>,
    );
  }
  if (asker) {
    const mine = viewer?.person_id === task.owner_person_id;
    const text = asker === viewerName ? "You assigned" : mine ? `Asked of you by ${asker}` : `${asker} asked`;
    parts.push(<span key="ask">{text}</span>);
  }
  const created = shortDue(task.created_at, new Date());
  if (created) parts.push(<span key="when">Added {created === "Today" ? "today" : created}</span>);
  if (task.notes) parts.push(<span key="notes">&ldquo;{task.notes}&rdquo;</span>);
  return (
    <p className="mt-0.5 text-sm text-fg-muted line-clamp-2">
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && " · "}
          {p}
        </span>
      ))}
    </p>
  );
}

function TaskEditor({
  draft,
  setDraft,
  people,
  onCancel,
  onSave,
  saving,
  error,
  submitLabel,
}: {
  draft: TaskDraft;
  setDraft: (d: TaskDraft) => void;
  people: Person[];
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  error: string | null;
  submitLabel: string;
}) {
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSave();
      }}
    >
      <input
        className={fieldCls}
        aria-label="Task"
        placeholder="What needs doing"
        value={draft.title}
        onChange={(e) => setDraft({ ...draft, title: e.target.value })}
        autoFocus
        maxLength={500}
      />
      <input
        className={fieldCls}
        aria-label="Notes"
        placeholder="Notes"
        value={draft.notes}
        onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
        maxLength={1000}
      />
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-sm text-fg-muted">
          Who
          <select
            className={selectCls}
            value={draft.owner_person_id}
            onChange={(e) => setDraft({ ...draft, owner_person_id: Number(e.target.value) })}
          >
            {people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.full_name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm text-fg-muted">
          Due
          <input
            type="date"
            className={selectCls}
            value={draft.due_date}
            onChange={(e) => setDraft({ ...draft, due_date: e.target.value })}
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-fg-muted">
          Status
          <select
            className={selectCls}
            value={draft.status}
            onChange={(e) => setDraft({ ...draft, status: e.target.value })}
          >
            {(["open", "in_progress", "waiting"] as const).map((s) => (
              <option key={s} value={s}>
                {TASK_STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
        <div className="ml-auto flex gap-2">
          <Button type="button" size="sm" onClick={onCancel} disabled={saving}>
            Cancel
          </Button>
          <Button type="submit" size="sm" variant="primary" disabled={saving || !draft.title.trim()}>
            {submitLabel}
          </Button>
        </div>
      </div>
      {error && <p className="text-sm text-rose-500">{error}</p>}
    </form>
  );
}

export default function TasksPage() {
  const [viewer, setViewer] = useState<PeopleViewer | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [scope, setScope] = useState<"mine" | "everyone">("everyone");
  const [personId, setPersonId] = useState<number | null>(null);
  const [state, setState] = useState<"open" | "done">("open");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<{ task: Task; draft: TaskDraft } | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState<TaskDraft | null>(null);
  const [addError, setAddError] = useState<string | null>(null);

  const isPrincipal = viewer?.is_principal ?? false;
  const viewerName = people.find((p) => p.id === viewer?.person_id)?.full_name ?? null;
  // Only the principal sees everyone's; anyone else sees their own.
  const owner = !isPrincipal || scope === "mine" ? viewer?.person_id ?? undefined : personId ?? undefined;

  useEffect(() => {
    getPeopleViewer()
      .then((v) => {
        setViewer(v);
        if (!v.is_principal) setScope("mine");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load"));
    listPeople()
      .then((all) => setPeople(all.filter((p) => p.kind === "team" && !p.archived)))
      .catch(() => {});
  }, []);

  const load = useCallback(
    (offset = 0) => {
      if (!viewer) return;
      setLoading(true);
      listTasks({ state, ownerPersonId: owner, limit: PAGE, offset })
        .then((r) => {
          setTasks((prev) => (offset ? [...prev, ...r.tasks] : r.tasks));
          setTotal(r.total);
          setError(null);
        })
        .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load tasks"))
        .finally(() => setLoading(false));
    },
    [viewer, state, owner],
  );

  useEffect(() => {
    load(0);
  }, [load]);

  const now = useMemo(() => new Date(), [tasks]); // eslint-disable-line react-hooks/exhaustive-deps
  const groups = useMemo(
    () => (state === "open" ? groupTasks(tasks, now) : [{ key: "done", label: "Done", tasks }]),
    [state, tasks, now],
  );

  function markBusy(id: number, on: boolean) {
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function close(t: Task, reason: "done" | "not_needed") {
    markBusy(t.task_id, true);
    try {
      await completeTask(t.task_id, reason);
      setTasks((prev) => prev.filter((x) => x.task_id !== t.task_id));
      setTotal((n) => Math.max(0, n - 1));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't close the task");
    } finally {
      markBusy(t.task_id, false);
    }
  }

  async function reopen(t: Task) {
    markBusy(t.task_id, true);
    try {
      await reopenTask(t.task_id);
      setTasks((prev) => prev.filter((x) => x.task_id !== t.task_id));
      setTotal((n) => Math.max(0, n - 1));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reopen the task");
    } finally {
      markBusy(t.task_id, false);
    }
  }

  async function saveEdit() {
    if (!editing) return;
    const { changes, base } = taskChanges(editing.task, editing.draft);
    if (Object.keys(changes).length === 0) {
      setEditing(null);
      return;
    }
    setSaving(true);
    setEditError(null);
    try {
      const updated = await updateTask(editing.task.task_id, changes, base);
      const stillShown = owner == null || updated.owner_person_id === owner;
      setTasks((prev) =>
        stillShown
          ? prev.map((x) => (x.task_id === updated.task_id ? updated : x))
          : prev.filter((x) => x.task_id !== updated.task_id),
      );
      setEditing(null);
    } catch (e) {
      if (e instanceof TaskConflict) {
        // Someone changed it meanwhile: show their copy, keep the edit open
        // on it so nothing is overwritten unseen.
        setTasks((prev) => prev.map((x) => (x.task_id === e.latest.task_id ? e.latest : x)));
        setEditing({ task: e.latest, draft: draftOf(e.latest) });
        setEditError("Someone changed this task while you were editing. Here is the latest. Make your change again.");
      } else {
        setEditError(e instanceof Error ? e.message : "Couldn't save the task");
      }
    } finally {
      setSaving(false);
    }
  }

  function startAdd() {
    setAddError(null);
    setAdding({
      title: "",
      notes: "",
      status: "open",
      due_date: "",
      owner_person_id: (scope === "everyone" && personId) || viewer?.person_id || people[0]?.id || 0,
    });
  }

  async function saveAdd() {
    if (!adding) return;
    setSaving(true);
    setAddError(null);
    try {
      const created = await createTask({
        task: adding.title.trim(),
        owner_person_id: adding.owner_person_id || undefined,
        due_date: adding.due_date || undefined,
        notes: adding.notes.trim() || undefined,
      });
      if (adding.status !== "open") {
        await updateTask(created.task_id, { status: adding.status as "in_progress" | "waiting" }, { status: "open" });
      }
      setAdding(null);
      if (state === "open") load(0);
      else setState("open");
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "Couldn't add the task");
    } finally {
      setSaving(false);
    }
  }

  if (!viewer && !error) return <PageSpinner />;

  return (
    <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl sm:text-3xl font-semibold text-fg">Tasks</h1>
          <p className="mt-1 text-fg-muted">
            What you and your team owe, from chats, meetings and what you assigned. Overdue ones get a follow-up.
          </p>
        </div>
        {viewer?.person_id != null && !adding && (
          <Button variant="primary" onClick={startAdd}>
            + Add task
          </Button>
        )}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        {isPrincipal && (
          <Segmented
            label="Whose tasks"
            value={scope}
            onChange={setScope}
            options={[
              { value: "mine", label: "Mine" },
              { value: "everyone", label: "Everyone" },
            ]}
          />
        )}
        {isPrincipal && scope === "everyone" && (
          <select
            aria-label="Person"
            className={selectCls}
            value={personId ?? ""}
            onChange={(e) => setPersonId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">All people</option>
            {people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.full_name}
              </option>
            ))}
          </select>
        )}
        <Segmented
          label="Open or done"
          value={state}
          onChange={(v) => {
            setEditing(null);
            setState(v);
          }}
          options={[
            { value: "open", label: "Open" },
            { value: "done", label: "Done" },
          ]}
        />
      </div>

      {adding && (
        <div className="mt-5 rounded-2xl border border-line bg-surface-overlay p-4">
          <TaskEditor
            draft={adding}
            setDraft={setAdding}
            people={people}
            onCancel={() => setAdding(null)}
            onSave={saveAdd}
            saving={saving}
            error={addError}
            submitLabel="Add task"
          />
        </div>
      )}

      {error && <p className="mt-4 text-sm text-rose-500">{error}</p>}

      {loading && tasks.length === 0 ? (
        <div className="mt-10">
          <PageSpinner />
        </div>
      ) : tasks.length === 0 ? (
        <p className="mt-10 text-center text-fg-muted">
          {state === "open"
            ? "No open tasks. Add one here, or ask in chat."
            : "Nothing finished in this list yet."}
        </p>
      ) : (
        groups.map((g) => (
          <section key={g.key} className="mt-7">
            {state === "open" && (
              <h2
                className={`mb-2 text-xs font-semibold tracking-wider uppercase ${
                  g.key === "overdue" ? "text-rose-500" : "text-fg-muted"
                }`}
              >
                {g.label} · {g.tasks.length}
              </h2>
            )}
            <ul className="rounded-2xl border border-line bg-surface divide-y divide-line overflow-hidden">
              {g.tasks.map((t) =>
                editing?.task.task_id === t.task_id ? (
                  <li key={t.task_id} className="p-4 bg-surface-overlay">
                    <TaskEditor
                      draft={editing.draft}
                      setDraft={(d) => setEditing({ task: editing.task, draft: d })}
                      people={people}
                      onCancel={() => setEditing(null)}
                      onSave={saveEdit}
                      saving={saving}
                      error={editError}
                      submitLabel="Save"
                    />
                  </li>
                ) : (
                  <TaskRow
                    key={t.task_id}
                    task={t}
                    viewer={viewer}
                    viewerName={viewerName}
                    overdue={g.key === "overdue"}
                    busy={busy.has(t.task_id)}
                    onDone={() => close(t, "done")}
                    onNotNeeded={() => close(t, "not_needed")}
                    onEdit={() => {
                      setEditError(null);
                      setEditing({ task: t, draft: draftOf(t) });
                    }}
                    onReopen={() => reopen(t)}
                  />
                ),
              )}
            </ul>
          </section>
        ))
      )}

      {tasks.length < total && (
        <div className="mt-5 flex justify-center">
          <Button onClick={() => load(tasks.length)} disabled={loading}>
            Show more
          </Button>
        </div>
      )}
    </div>
  );
}

function TaskRow({
  task,
  viewer,
  viewerName,
  overdue,
  busy,
  onDone,
  onNotNeeded,
  onEdit,
  onReopen,
}: {
  task: Task;
  viewer: PeopleViewer | null;
  viewerName: string | null;
  overdue: boolean;
  busy: boolean;
  onDone: () => void;
  onNotNeeded: () => void;
  onEdit: () => void;
  onReopen: () => void;
}) {
  const done = task.status === "done";
  const menu: OverflowItem[] = [];
  if (!done) menu.push({ label: "Not needed", onSelect: onNotNeeded, disabled: busy });
  if (task.session_id) menu.push({ label: "Open the chat it came from", href: `/?session=${encodeURIComponent(task.session_id)}` });
  const when = done ? shortDue(task.completed_at, new Date()) : shortDue(task.due_at, new Date());
  const badge = STATUS_BADGE[task.status];
  return (
    <li className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 px-4 py-3">
      <div className="flex items-start gap-3 min-w-0 flex-1 break-words">
        {!done && (
          <input
            type="checkbox"
            aria-label={`Mark "${taskTitle(task)}" done`}
            className="mt-1 h-5 w-5 shrink-0 rounded border-line"
            checked={false}
            disabled={busy}
            onChange={onDone}
          />
        )}
        <div className="min-w-0">
          <p className={`text-[15px] font-medium ${done ? "text-fg-muted line-through" : "text-fg"}`}>
            {taskTitle(task)}
            {badge && (
              <span className={`ml-2 align-middle text-xs font-medium px-2 py-0.5 rounded-md ${badge}`}>
                {TASK_STATUS_LABEL[task.status]}
              </span>
            )}
          </p>
          <SourceLine task={task} viewer={viewer} viewerName={viewerName} />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 sm:gap-4 pl-8 sm:pl-0 sm:shrink-0">
        <span className="sm:w-28 truncate text-sm text-fg">{ownerLabel(task, viewer)}</span>
        <span className={`sm:w-24 text-sm ${overdue ? "text-rose-500" : "text-fg-muted"}`}>
          {done && when ? `Done ${when === "Today" || when === "Tomorrow" ? when.toLowerCase() : when}` : when}
        </span>
        <span className="ml-auto sm:hidden" />
        {done ? (
          <Button size="sm" onClick={onReopen} disabled={busy}>
            Reopen
          </Button>
        ) : (
          <>
            <Button size="sm" onClick={onDone} disabled={busy}>
              Done
            </Button>
            <Button size="sm" variant="ghost" onClick={onEdit} disabled={busy}>
              Edit
            </Button>
          </>
        )}
        {menu.length > 0 && <OverflowMenu items={menu} size="sm" label="More for this task" />}
      </div>
    </li>
  );
}
