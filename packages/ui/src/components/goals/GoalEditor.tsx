"use client";

import { useEffect, useRef, useState } from "react";

import TimeframePicker, { suggestPeriodValue } from "@/components/TimeframePicker";
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

function cls(...parts: (string | false | undefined)[]) {
  return parts.filter(Boolean).join(" ");
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
          <div className="text-xs text-fg-muted mt-0.5">
            Target: {goal.target}
            {goal.current ? ` — Current: ${goal.current}` : ""}
          </div>
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
        <div className="flex flex-col items-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
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
                if (!window.confirm("Delete this Goal?")) return;
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

  return (
    <div className="py-3 border-b border-line last:border-0 space-y-2">
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
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
        >
          {GOAL_STATUS_OPTS.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </select>
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Key result
        <input
          value={form.key_result}
          onChange={(e) => setForm((f) => ({ ...f, key_result: e.target.value }))}
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          placeholder="Close Series A by Jun 30"
        />
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Target
        <input
          value={form.target}
          onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))}
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          placeholder="What does done look like?"
        />
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Current
        <input
          value={form.current}
          onChange={(e) => setForm((f) => ({ ...f, current: e.target.value }))}
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          placeholder="Where are we now?"
        />
      </label>
      {err && <p className="text-xs text-rose-300">{err}</p>}
      <div className="flex gap-2">
        <button
          disabled={saving}
          onClick={async () => {
            setSaving(true);
            setErr(null);
            try {
              const updated = await updateGoal(slug, goal.id!, form);
              onSaved(updated);
              setEditingAndNotify(false);
            } catch (e) {
              setErr(e instanceof Error ? e.message : "Save failed");
            } finally {
              setSaving(false);
            }
          }}
          className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          disabled={saving}
          onClick={() => {
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
          }}
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
  /** When set, an "Area" picker over these lets the user choose where the goal goes. */
  areas?: { slug: string; title: string }[];
  onCreated: (goal: Goal) => void;
  onCancel: () => void;
}

export function AddGoalForm({ slug, areas, onCreated, onCancel }: AddGoalFormProps) {
  const [areaSlug, setAreaSlug] = useState(slug);
  const [form, setForm] = useState<{
    period_type: PeriodType;
    period_value: string;
    key_result: string;
    target: string;
    current: string;
    status: GoalStatus;
  }>(() => ({
    period_type: "quarter",
    period_value: suggestPeriodValue("quarter"),
    key_result: "",
    target: "",
    current: "",
    status: "on_track",
  }));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const firstRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    firstRef.current?.focus();
  }, []);

  return (
    <div className="py-3 border-b border-line space-y-2 bg-surface-overlay/30 px-4 -mx-4 rounded-lg">
      <div className="text-xs font-semibold text-fg-muted uppercase tracking-wide mb-1">New Goal</div>
      {areas && (
        <label className="text-xs text-fg-muted flex flex-col gap-1">
          Area
          <select
            value={areaSlug}
            onChange={(e) => setAreaSlug(e.target.value)}
            className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          >
            {areas.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.title}
              </option>
            ))}
          </select>
        </label>
      )}
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
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
        >
          {GOAL_STATUS_OPTS.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </select>
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Key result
        <input
          ref={firstRef}
          value={form.key_result}
          onChange={(e) => setForm((f) => ({ ...f, key_result: e.target.value }))}
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          placeholder="What do we want to achieve?"
        />
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Target
        <input
          value={form.target}
          onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))}
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          placeholder="Measurable target"
        />
      </label>
      <label className="text-xs text-fg-muted flex flex-col gap-1">
        Current (optional)
        <input
          value={form.current}
          onChange={(e) => setForm((f) => ({ ...f, current: e.target.value }))}
          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
          placeholder="Current progress"
        />
      </label>
      {err && <p className="text-xs text-rose-300">{err}</p>}
      <div className="flex gap-2">
        <button
          disabled={saving || !form.period_value || !form.key_result || !form.target}
          onClick={async () => {
            setSaving(true);
            setErr(null);
            try {
              const goal = await createGoal(areaSlug, form);
              onCreated(goal);
            } catch (e) {
              setErr(e instanceof Error ? e.message : "Create failed");
            } finally {
              setSaving(false);
            }
          }}
          className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50"
        >
          {saving ? "Creating…" : "Add Goal"}
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
