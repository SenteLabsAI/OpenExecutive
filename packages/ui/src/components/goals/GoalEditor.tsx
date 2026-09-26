"use client";

import { useEffect, useRef, useState } from "react";

import TimeframePicker, { TimeframeChips, suggestPeriodValue } from "@/components/TimeframePicker";
import {
  createGoal,
  deleteGoal,
  updateGoal,
  type Goal,
  type PeriodType,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";

// Goal rows (view / edit / delete) and the add-goal form, shared by a
// department's page and /goals. Goals belong to a department — an "area" in
// solo mode — and every call here is scoped by its slug.

// "Last reviewed >N days ago" → render the row with a stale accent.
// Healthy departments have `daily@09:00` so nothing should ever exceed 1d;
// 7d catches departments that drift well past their cadence.
const STALE_REVIEW_DAYS = 7;
const STALE_REVIEW_MS = STALE_REVIEW_DAYS * 24 * 60 * 60 * 1000;

function isStaleReview(lastReviewedAt: string): boolean {
  if (!lastReviewedAt) return false; // "Never reviewed" rendered separately
  const ts = new Date(lastReviewedAt).getTime();
  if (!Number.isFinite(ts)) return false;
  return Date.now() - ts > STALE_REVIEW_MS;
}

export const GOAL_STATUS_OPTS = ["on_track", "at_risk", "off_track"] as const;
export type GoalStatus = (typeof GOAL_STATUS_OPTS)[number];

export const GOAL_STATUS_COLORS: Record<string, string> = {
  on_track: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
  at_risk: "bg-amber-500/20 text-amber-300 border-amber-500/30",
  off_track: "bg-rose-500/20 text-rose-300 border-rose-500/30",
};

const INPUT_CLS =
  "px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500";

const GOAL_PLACEHOLDER = "e.g. Close Series A";
const TARGET_PLACEHOLDER = "How will you know it's done? e.g. $5M raised";
const CURRENT_PLACEHOLDER = "e.g. $2M committed";

function cls(...parts: (string | false | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

// Enter submits, Escape cancels — for the single-line inputs of both forms.
function formKeys(onSubmit: () => void, onCancel: () => void) {
  return (e: React.KeyboardEvent<HTMLElement>) => {
    if (e.key === "Enter" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      onSubmit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    }
  };
}

// ---------------------------------------------------------------------------
// Goal row — view or edit
// ---------------------------------------------------------------------------

interface GoalRowProps {
  slug: string;
  goal: Goal;
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

export function GoalRow({ slug, goal, onSaved, onDeleted, onEditingChange }: GoalRowProps) {
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
    return (
      <div
        className={cls(
          "flex items-start gap-3 py-3 border-b border-line last:border-0 group",
          stale && "border-l-2 border-l-amber-500/60 pl-3 -ml-3"
        )}
      >
        <span
          className={cls(
            "mt-0.5 flex-shrink-0 inline-block px-2 py-0.5 rounded border text-[10px] font-medium",
            GOAL_STATUS_COLORS[goal.status]
          )}
        >
          {goal.status.replace("_", " ")}
        </span>
        <div className="flex-1 min-w-0">
          <div className="text-sm text-fg font-medium">{goal.key_result}</div>
          {progress && <div className="text-xs text-fg-muted mt-0.5">{progress}</div>}
          <div className="text-xs text-fg-subtle mt-0.5 flex items-center gap-2 flex-wrap">
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
        {/* Always shown on small screens (no hover on touch); revealed on
            hover or keyboard focus from md up. */}
        <div className="flex flex-col items-end gap-1 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100 transition-opacity flex-shrink-0">
          <div className="flex gap-1">
            <button
              onClick={() => setEditingAndNotify(true)}
              className="px-2 py-1 text-xs rounded bg-surface-overlay hover:bg-surface-input border border-line"
            >
              Edit
            </button>
            <button
              disabled={deleting}
              onClick={async () => {
                if (!window.confirm("Delete this goal?")) return;
                setDeleting(true);
                setErr(null);
                try {
                  await deleteGoal(slug, goal.id!);
                  onDeleted(goal.id!);
                } catch (e) {
                  setErr(e instanceof Error ? e.message : "Delete failed");
                  setDeleting(false);
                }
              }}
              className="px-2 py-1 text-xs rounded bg-rose-500/10 hover:bg-rose-500/20 border border-rose-500/30 text-rose-300 disabled:opacity-50"
            >
              {deleting ? "…" : "Delete"}
            </button>
          </div>
          {err && <span className="text-[10px] text-rose-300">{err}</span>}
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
    setEditingAndNotify(false);
    setErr(null);
  }

  async function save() {
    if (!canSave) return;
    setSaving(true);
    setErr(null);
    try {
      const updated = await updateGoal(slug, goal.id!, {
        ...form,
        key_result: form.key_result.trim(),
        period_value: form.period_value.trim(),
        target: form.target.trim(),
        current: form.current.trim(),
      });
      onSaved(updated);
      setEditingAndNotify(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  const onKeyDown = formKeys(save, cancel);

  return (
    <div className="py-3 border-b border-line last:border-0 space-y-2">
      <label className="text-xs text-fg-muted flex flex-col gap-1">
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
      <label className="text-xs text-fg-muted flex flex-col gap-1">
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
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Target (optional)
        <input
          value={form.target}
          onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))}
          onKeyDown={onKeyDown}
          className={INPUT_CLS}
          placeholder={TARGET_PLACEHOLDER}
        />
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Where it stands now (optional)
        <input
          value={form.current}
          onChange={(e) => setForm((f) => ({ ...f, current: e.target.value }))}
          onKeyDown={onKeyDown}
          className={INPUT_CLS}
          placeholder={CURRENT_PLACEHOLDER}
        />
      </label>
      {err && <p className="text-xs text-rose-300">{err}</p>}
      <div className="flex gap-2">
        <button
          disabled={!canSave}
          onClick={save}
          className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          disabled={saving}
          onClick={cancel}
          className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add Goal form
// ---------------------------------------------------------------------------

interface AddGoalFormProps {
  /** The department (area) the goal is added to — the picker's first choice when `areas` is set. */
  slug: string;
  /** When set, a picker over these lets the user choose where the goal goes. */
  areas?: { slug: string; title: string }[];
  /** The picker's label: "Area" (solo) or "Department" (team). */
  areaLabel?: string;
  onCreated: (goal: Goal) => void;
  onCancel: () => void;
}

// One thing to type — the goal. The timeframe is a chip (this quarter by
// default), target and current are optional, and a new goal starts on track
// until the department's check-in grades it.
export function AddGoalForm({ slug, areas, areaLabel = "Area", onCreated, onCancel }: AddGoalFormProps) {
  const [areaSlug, setAreaSlug] = useState(slug);
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

  const canSubmit = !saving && form.key_result.trim() !== "";

  async function submit() {
    if (!canSubmit) return;
    setSaving(true);
    setErr(null);
    try {
      const target = form.target.trim();
      const current = form.current.trim();
      const goal = await createGoal(areaSlug, {
        period_type: form.period_type,
        period_value: form.period_value,
        key_result: form.key_result.trim(),
        ...(target && { target }),
        ...(current && { current }),
      });
      onCreated(goal);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSaving(false);
    }
  }

  const onKeyDown = formKeys(submit, onCancel);

  return (
    <div className="py-3 border-b border-line space-y-3 bg-surface-overlay/30 px-4 -mx-4 rounded-lg">
      <div className="text-xs font-semibold text-fg-muted uppercase tracking-wide">New goal</div>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
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
      {areas && areas.length > 1 && (
        <label className="text-xs text-fg-muted flex flex-col gap-1">
          {areaLabel}
          <select
            value={areaSlug}
            onChange={(e) => setAreaSlug(e.target.value)}
            className={INPUT_CLS}
          >
            {areas.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.title}
              </option>
            ))}
          </select>
        </label>
      )}
      <TimeframeChips
        periodType={form.period_type}
        onChange={(pt, pv) => setForm((f) => ({ ...f, period_type: pt, period_value: pv }))}
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <label className="text-xs text-fg-muted flex flex-col gap-1">
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
        <label className="text-xs text-fg-muted flex flex-col gap-1">
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
      {err && <p className="text-xs text-rose-300">{err}</p>}
      <div className="flex gap-2">
        <button
          disabled={!canSubmit}
          onClick={submit}
          className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50"
        >
          {saving ? "Adding…" : "Add goal"}
        </button>
        <button
          disabled={saving}
          onClick={onCancel}
          className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
