"use client";

import { useEffect, useRef, useState } from "react";

import TimeframePicker, { TimeframeChips, suggestPeriodValue } from "@/components/TimeframePicker";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import {
  createGoal,
  createGoalInArea,
  deleteAnyGoal,
  deleteGoal,
  updateAnyGoal,
  updateGoal,
  type Goal,
  type GoalPatch,
  type PeriodType,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";

// Goal rows (view / edit / delete) and the add-goal form, shared by a
// department's page and /goals. A goal has an owner and, optionally, a
// department (an "area" in solo mode). On a department's page every call is
// scoped by its slug; on /goals (slug null) they go to the department-free
// goal routes, which can also change a goal's owner and department.

// "Last reviewed >N days ago" → render the row with a stale accent.
// A new department checks in weekly (`weekly@mon@09:00`), so a healthy goal
// is reviewed at most ~7d ago; 10d leaves room for a skipped or late
// check-in and still catches departments that drift well past their cadence.
const STALE_REVIEW_DAYS = 10;
const STALE_REVIEW_MS = STALE_REVIEW_DAYS * 24 * 60 * 60 * 1000;

function isStaleReview(lastReviewedAt: string): boolean {
  if (!lastReviewedAt) return false; // "Never reviewed" rendered separately
  const ts = new Date(lastReviewedAt).getTime();
  if (!Number.isFinite(ts)) return false;
  return Date.now() - ts > STALE_REVIEW_MS;
}

export const GOAL_STATUS_OPTS = ["on_track", "at_risk", "off_track"] as const;
export type GoalStatus = (typeof GOAL_STATUS_OPTS)[number];

// A goal's status as a dot and a word, the one status pill the goal screens
// share (a goal row, an area's summary on /goals, a department card).
const GOAL_STATUS_DOT: Record<string, string> = {
  on_track: "bg-emerald-500",
  at_risk: "bg-amber-500",
  off_track: "bg-rose-500",
};

export function goalStatusLabel(status: string): string {
  return status.replace("_", " ");
}

export function GoalStatusPill({ status, count }: { status: string; count?: number }) {
  return (
    <span className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-full border border-line bg-surface-elevated px-2.5 py-1 text-[13px] font-medium text-fg whitespace-nowrap">
      <span aria-hidden="true" className={cls("h-2 w-2 rounded-full", GOAL_STATUS_DOT[status] ?? "bg-fg-subtle")} />
      {count !== undefined && <span>{count}</span>}
      {goalStatusLabel(status)}
    </span>
  );
}

const INPUT_CLS =
  "h-11 px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";
const LABEL_CLS = "text-sm text-fg-muted flex flex-col gap-1.5";

const GOAL_PLACEHOLDER = "e.g. Close Series A";
const TARGET_PLACEHOLDER = "How will you know it's done? e.g. $5M raised";
const CURRENT_PLACEHOLDER = "e.g. $2M committed";

function cls(...parts: (string | false | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

// Enter submits, Escape cancels — for the single-line inputs of both forms.
// Escape is ignored mid-save, like the disabled Cancel button, so a request
// in flight can't land on a form the user already closed.
function formKeys(onSubmit: () => void, onCancel: () => void, saving: boolean) {
  return (e: React.KeyboardEvent<HTMLElement>) => {
    if (e.key === "Enter" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      onSubmit();
    } else if (e.key === "Escape" && !saving) {
      e.preventDefault();
      onCancel();
    }
  };
}

// ---------------------------------------------------------------------------
// Goal row — view or edit
// ---------------------------------------------------------------------------

/** A person a goal can be on, for the owner pickers. */
export interface OwnerOption {
  id: number;
  name: string;
}

/** A department a goal can be in, for the department pickers. */
export interface DepartmentOption {
  slug: string;
  title: string;
}

interface GoalRowProps {
  /** The department whose page this is, or null on /goals. */
  slug: string | null;
  goal: Goal;
  /** One line under the goal, e.g. "Dan · Sales" (on /goals). */
  meta?: string;
  /** With slug null: the edit form also picks the owner and department. */
  owners?: OwnerOption[];
  /** The goal's owner as shown (its own, else its lead's or the principal's). */
  ownerId?: number | null;
  departments?: DepartmentOption[];
  /** "Department" (team) or "Area" (solo). */
  departmentLabel?: string;
  onSaved: (updated: Goal) => void;
  onDeleted: (id: number) => void;
  // Surface edit-mode transitions so the parent can pause polling — a
  // server snapshot replacing `goals` while a user is mid-edit would
  // flicker the view label and discard the form state.
  onEditingChange?: (editing: boolean) => void;
}

export function formatGoalPeriod(g: Goal): string {
  if (g.period_type === "ongoing") return g.period_value || "Ongoing";
  return `${g.period_type.charAt(0).toUpperCase() + g.period_type.slice(1)}: ${g.period_value}`;
}

// "Target: $5M — Current: $2M", either half alone, or "" when neither is set.
export function formatGoalProgress(g: Pick<Goal, "target" | "current">): string {
  if (g.target) return `Target: ${g.target}${g.current ? ` — Current: ${g.current}` : ""}`;
  return g.current ? `Current: ${g.current}` : "";
}

export function GoalRow({
  slug,
  goal,
  meta,
  owners,
  ownerId = null,
  departments,
  departmentLabel = "Department",
  onSaved,
  onDeleted,
  onEditingChange,
}: GoalRowProps) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState({
    period_type: goal.period_type,
    period_value: goal.period_value,
    key_result: goal.key_result,
    target: goal.target,
    current: goal.current,
    status: goal.status as GoalStatus,
  });
  // Owner and department, editable on /goals only. "" = no department.
  const [owner, setOwner] = useState<number | null>(ownerId);
  const [dept, setDept] = useState(goal.department_slug ?? "");
  const canMove = slug === null;

  // Centralise the editing transition so save/cancel/enter all notify
  // the parent — avoids forgetting the call in one branch.
  function setEditingAndNotify(next: boolean) {
    setEditing(next);
    onEditingChange?.(next);
  }

  // Belt-and-suspenders: if the row unmounts while still in edit mode
  // (e.g. parent replaces the goals list and drops this row), the
  // parent's edit counter would otherwise stay incremented and pause
  // polling forever. Read latest `editing` via a ref so the unmount
  // cleanup sees the current value, not the value captured at mount.
  // The parent's Math.max(0, …) guards against a double-decrement if
  // the row also ran its own cancel path before unmount.
  const editingRef = useRef(editing);
  useEffect(() => {
    editingRef.current = editing;
  }, [editing]);
  useEffect(() => {
    return () => {
      if (editingRef.current) onEditingChange?.(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stale = isStaleReview(goal.last_reviewed_at);

  if (!editing) {
    const progress = formatGoalProgress(goal);
    async function remove() {
      if (!window.confirm("Delete this goal?")) return;
      setDeleting(true);
      setErr(null);
      try {
        if (slug === null) await deleteAnyGoal(goal.id!);
        else await deleteGoal(slug, goal.id!);
        onDeleted(goal.id!);
      } catch (e) {
        setErr(e instanceof Error ? e.message : "Delete failed");
        setDeleting(false);
      }
    }
    return (
      <div
        className={cls(
          "flex items-start gap-3 sm:gap-4 py-4 border-b border-line last:border-0",
          stale && "border-l-2 border-l-amber-500/60 pl-3 -ml-3"
        )}
      >
        <div className="flex-1 min-w-0">
          <div className="text-base text-fg font-medium leading-snug">{goal.key_result}</div>
          {progress && <div className="text-[15px] text-fg-muted mt-1">{progress}</div>}
          {meta && <div className="text-sm text-fg-muted mt-1">{meta}</div>}
          <div className="text-sm text-fg-subtle mt-2 flex items-center gap-x-2 gap-y-1.5 flex-wrap">
            <GoalStatusPill status={goal.status} />
            <span>{formatGoalPeriod(goal)}</span>
            <span aria-hidden="true">·</span>
            {goal.last_reviewed_at ? (
              <span className={cls(stale && "text-amber-400")}>
                Last reviewed {formatRelativeTime(goal.last_reviewed_at)}
              </span>
            ) : (
              <span className="italic">Never reviewed</span>
            )}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1 flex-shrink-0 -mr-2 -mt-1.5">
          <OverflowMenu
            label={`Actions for goal: ${goal.key_result}`}
            items={[
              { label: "Edit goal", onSelect: () => setEditingAndNotify(true) },
              { label: deleting ? "Deleting…" : "Delete goal", danger: true, disabled: deleting, onSelect: () => void remove() },
            ]}
          />
          {err && <span className="text-xs text-rose-500">{err}</span>}
        </div>
      </div>
    );
  }

  const canSave = !saving && form.key_result.trim() !== "" && form.period_value.trim() !== "";

  function cancel() {
    setForm({
      period_type: goal.period_type,
      period_value: goal.period_value,
      key_result: goal.key_result,
      target: goal.target,
      current: goal.current,
      status: goal.status as GoalStatus,
    });
    setOwner(ownerId);
    setDept(goal.department_slug ?? "");
    setEditingAndNotify(false);
    setErr(null);
  }

  async function save() {
    if (!canSave) return;
    setSaving(true);
    setErr(null);
    try {
      const patch: GoalPatch = {
        ...form,
        key_result: form.key_result.trim(),
        period_value: form.period_value.trim(),
        target: form.target.trim(),
        current: form.current.trim(),
      };
      let updated: Goal;
      if (slug === null) {
        // Only what changed, so an untouched owner keeps following its lead.
        if (owner !== ownerId) patch.owner_person_id = owner;
        if (dept !== (goal.department_slug ?? "")) patch.department_slug = dept || null;
        updated = await updateAnyGoal(goal.id!, patch);
      } else {
        updated = await updateGoal(slug, goal.id!, patch);
      }
      onSaved(updated);
      setEditingAndNotify(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  const onKeyDown = formKeys(save, cancel, saving);

  return (
    <div className="py-4 border-b border-line last:border-0 space-y-3">
      <label className={LABEL_CLS}>
        Goal
        <input
          value={form.key_result}
          onChange={(e) => setForm((f) => ({ ...f, key_result: e.target.value }))}
          onKeyDown={onKeyDown}
          className={INPUT_CLS}
          placeholder={GOAL_PLACEHOLDER}
        />
      </label>
      <TimeframePicker
        periodType={form.period_type}
        periodValue={form.period_value}
        onChange={(pt, pv) => setForm((f) => ({ ...f, period_type: pt, period_value: pv }))}
        size="compact"
      />
      {canMove && owners && owners.length > 1 && (
        <label className={LABEL_CLS}>
          Owner
          <select
            value={owner ?? ""}
            onChange={(e) => setOwner(e.target.value ? Number(e.target.value) : null)}
            className={INPUT_CLS}
          >
            {owner == null && <option value="">No owner yet</option>}
            {owners.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {canMove && departments && departments.length > 0 && (
        <label className={LABEL_CLS}>
          {departmentLabel}
          <select value={dept} onChange={(e) => setDept(e.target.value)} className={INPUT_CLS}>
            <option value="">None</option>
            {departments.map((d) => (
              <option key={d.slug} value={d.slug}>
                {d.title}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className={LABEL_CLS}>
        Status
        <select
          value={form.status}
          onChange={(e) => setForm((f) => ({ ...f, status: e.target.value as GoalStatus }))}
          className={INPUT_CLS}
        >
          {GOAL_STATUS_OPTS.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </select>
      </label>
      <label className={LABEL_CLS}>
        Target (optional)
        <input
          value={form.target}
          onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))}
          onKeyDown={onKeyDown}
          className={INPUT_CLS}
          placeholder={TARGET_PLACEHOLDER}
        />
      </label>
      <label className={LABEL_CLS}>
        Where it stands now (optional)
        <input
          value={form.current}
          onChange={(e) => setForm((f) => ({ ...f, current: e.target.value }))}
          onKeyDown={onKeyDown}
          className={INPUT_CLS}
          placeholder={CURRENT_PLACEHOLDER}
        />
      </label>
      {err && <p className="text-sm text-rose-500">{err}</p>}
      <div className="flex gap-2">
        <Button variant="primary" disabled={!canSave} onClick={save}>
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button disabled={saving} onClick={cancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add Goal form
// ---------------------------------------------------------------------------

interface AddGoalFormProps {
  /** The department whose page this is: the goal goes there. Null on /goals,
   * where the form picks an owner and, optionally, a department. */
  slug: string | null;
  /** /goals: the departments to pick from ("None" and "New…" are added). */
  departments?: DepartmentOption[];
  /** /goals: the people a goal can be on; the picker shows for two or more. */
  owners?: OwnerOption[];
  /** /goals: the owner picked at first (the viewer). */
  defaultOwnerId?: number | null;
  /** The department picker's label: "Area" (solo) or "Department" (team). */
  areaLabel?: string;
  /** Titled as the first goal, with a line on how little it takes. */
  first?: boolean;
  onCreated: (goal: Goal) => void;
  /** Omitted when the form is the page's only content (no Cancel then). */
  onCancel?: () => void;
}

// The "New…" choice in the department picker.
const NEW_DEPARTMENT = "__new__";

// One thing to type — the goal. The timeframe is a chip (this quarter by
// default), target and current are optional, and a new goal starts on track
// until its check-in (or the weekly goal review) grades it.
export function AddGoalForm({
  slug,
  departments = [],
  owners = [],
  defaultOwnerId = null,
  areaLabel = "Department",
  first = false,
  onCreated,
  onCancel,
}: AddGoalFormProps) {
  // "" = no department; NEW_DEPARTMENT = the typed name below.
  const [deptChoice, setDeptChoice] = useState("");
  const [newDept, setNewDept] = useState("");
  const [ownerId, setOwnerId] = useState<number | null>(defaultOwnerId ?? owners[0]?.id ?? null);
  const [form, setForm] = useState<{
    period_type: PeriodType;
    period_value: string;
    key_result: string;
    target: string;
    current: string;
  }>(() => ({
    period_type: "quarter",
    period_value: suggestPeriodValue("quarter"),
    key_result: "",
    target: "",
    current: "",
  }));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const firstRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    firstRef.current?.focus();
  }, []);

  const canSubmit =
    !saving &&
    form.key_result.trim() !== "" &&
    (deptChoice !== NEW_DEPARTMENT || newDept.trim() !== "");

  async function submit() {
    if (!canSubmit) return;
    setSaving(true);
    setErr(null);
    try {
      const target = form.target.trim();
      const current = form.current.trim();
      const body = {
        period_type: form.period_type,
        period_value: form.period_value,
        key_result: form.key_result.trim(),
        ...(target && { target }),
        ...(current && { current }),
      };
      let goal: Goal;
      if (slug) {
        goal = await createGoal(slug, body);
      } else {
        const area = deptChoice === NEW_DEPARTMENT ? newDept.trim() : deptChoice;
        goal = await createGoalInArea(area, {
          ...body,
          ...(owners.length > 1 && ownerId != null && { owner_person_id: ownerId }),
        });
      }
      onCreated(goal);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSaving(false);
    }
  }

  const onKeyDown = formKeys(submit, onCancel ?? (() => {}), saving);

  return (
    <div className="py-5 space-y-4">
      {first ? (
        <div>
          <div className="text-base font-semibold text-fg">Add your first goal</div>
          <p className="mt-1 text-[15px] text-fg-muted">
            Just type it. The timeframe defaults to this quarter, and I&apos;ll check it weekly.
          </p>
        </div>
      ) : (
        <div className="text-base font-semibold text-fg">New goal</div>
      )}
      <label className={LABEL_CLS}>
        What&apos;s the goal?
        <input
          ref={firstRef}
          value={form.key_result}
          onChange={(e) => setForm((f) => ({ ...f, key_result: e.target.value }))}
          onKeyDown={onKeyDown}
          maxLength={512}
          className={INPUT_CLS}
          placeholder={GOAL_PLACEHOLDER}
        />
      </label>
      {!slug && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {owners.length > 1 && (
            <label className={LABEL_CLS}>
              Owner
              <select
                value={ownerId ?? ""}
                onChange={(e) => setOwnerId(e.target.value ? Number(e.target.value) : null)}
                className={INPUT_CLS}
              >
                {owners.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className={LABEL_CLS}>
            {areaLabel} (optional)
            <select value={deptChoice} onChange={(e) => setDeptChoice(e.target.value)} className={INPUT_CLS}>
              <option value="">None</option>
              {departments.map((d) => (
                <option key={d.slug} value={d.slug}>
                  {d.title}
                </option>
              ))}
              <option value={NEW_DEPARTMENT}>New {areaLabel.toLowerCase()}…</option>
            </select>
          </label>
          {deptChoice === NEW_DEPARTMENT && (
            <label className={cls(LABEL_CLS, "sm:col-span-2")}>
              New {areaLabel.toLowerCase()}&apos;s name
              <input
                value={newDept}
                onChange={(e) => setNewDept(e.target.value)}
                onKeyDown={onKeyDown}
                maxLength={80}
                className={INPUT_CLS}
                placeholder="e.g. Sales or Product"
              />
            </label>
          )}
        </div>
      )}
      <TimeframeChips
        periodType={form.period_type}
        onChange={(pt, pv) => setForm((f) => ({ ...f, period_type: pt, period_value: pv }))}
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className={LABEL_CLS}>
          Target (optional)
          <input
            value={form.target}
            onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))}
            onKeyDown={onKeyDown}
            maxLength={512}
            className={INPUT_CLS}
            placeholder={TARGET_PLACEHOLDER}
          />
        </label>
        <label className={LABEL_CLS}>
          Where it stands now (optional)
          <input
            value={form.current}
            onChange={(e) => setForm((f) => ({ ...f, current: e.target.value }))}
            onKeyDown={onKeyDown}
            maxLength={512}
            className={INPUT_CLS}
            placeholder={CURRENT_PLACEHOLDER}
          />
        </label>
      </div>
      {err && <p className="text-sm text-rose-500">{err}</p>}
      <div className="flex gap-2">
        <Button variant="primary" disabled={!canSubmit} onClick={submit}>
          {saving ? "Adding…" : "Add goal"}
        </Button>
        {onCancel && (
          <Button disabled={saving} onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </div>
  );
}
