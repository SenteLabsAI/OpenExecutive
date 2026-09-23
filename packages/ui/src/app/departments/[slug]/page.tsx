"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import TimeframePicker, { suggestPeriodValue } from "@/components/TimeframePicker";
import {
  createGoal,
  deleteGoal,
  deleteDepartment,
  getDepartment,
  listPeople,
  updateDepartment,
  updateGoal,
  type DepartmentConfig,
  type DepartmentState,
  type Goal,
  type Person,
  type PeriodType,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";

// Auto-refresh cadence for the detail page. The `dept_cadence` scheduler
// fires at most once per department per cadence (default daily), so any
// poll faster than ~30s is overkill for review-driven status changes
// but keeps the page feeling live for cross-tab edits.
const POLL_INTERVAL_MS = 30_000;
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const STATUS_OPTS = ["on_track", "at_risk", "off_track"] as const;
type GoalStatus = (typeof STATUS_OPTS)[number];

const STATUS_COLORS: Record<string, string> = {
  on_track: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
  at_risk: "bg-amber-500/20 text-amber-300 border-amber-500/30",
  off_track: "bg-rose-500/20 text-rose-300 border-rose-500/30",
};

const AUTHORITY_OPTS: DepartmentConfig["authority_level"][] = [
  "auto_execute",
  "propose_only",
  "escalate",
];

const AUTHORITY_META: Record<
  DepartmentConfig["authority_level"],
  { label: string; hint: string }
> = {
  auto_execute: {
    label: "Acts on its own",
    hint: "The specialist runs actions in its scope without asking. You'll see them in the audit log.",
  },
  propose_only: {
    label: "Proposes, you approve",
    hint: "The specialist drafts actions and routes them to a person for approval before anything happens.",
  },
  escalate: {
    label: "Escalates to a human",
    hint: "The specialist will not act — it forwards everything to a human.",
  },
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

function _formatPeriodLabel(g: Goal): string {
  if (g.period_type === "ongoing") return g.period_value || "Ongoing";
  return `${g.period_type.charAt(0).toUpperCase() + g.period_type.slice(1)}: ${g.period_value}`;
}

function GoalRow({ slug, goal, onSaved, onDeleted, onEditingChange }: GoalRowProps) {
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
            STATUS_COLORS[goal.status]
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
            <span>{_formatPeriodLabel(goal)}</span>
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
          {STATUS_OPTS.map((s) => (
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
  slug: string;
  onCreated: (goal: Goal) => void;
  onCancel: () => void;
}

function AddGoalForm({ slug, onCreated, onCancel }: AddGoalFormProps) {
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
          {STATUS_OPTS.map((s) => (
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
              const goal = await createGoal(slug, form);
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

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function DepartmentDetailPage() {
  const params = useParams();
  const slug = params?.slug as string;
  const router = useRouter();

  const [dept, setDept] = useState<DepartmentState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // People for the head-person picker (loaded lazily)
  const [people, setPeople] = useState<Person[]>([]);

  // Settings edit state
  const [editingSettings, setEditingSettings] = useState(false);
  const [settingsForm, setSettingsForm] = useState({
    authority_level: "propose_only" as DepartmentConfig["authority_level"],
    mission: "",
    cadences: {} as Record<string, string>,
    headcount: "",
    budget_usd: "",
    head_person_id: null as number | null,
    slack_channel_id: "",
    discord_channel_id: "",
    telegram_chat_id: "",
    // One entity per line in the textarea; split + trimmed on save.
    watched_entities: "",
  });
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsErr, setSettingsErr] = useState<string | null>(null);

  // Delete state
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);

  // Goal state
  const [goals, setGoals] = useState<Goal[]>([]);
  const [addingGoal, setAddingGoal] = useState(false);

  // Count of GoalRows currently in edit mode. Polling pauses while > 0
  // so a snapshot replacing `goals` mid-edit doesn't flicker the view
  // label or wipe the form state. The refs let the poll callback always
  // read the live values without re-binding the interval — keeping the
  // assignment in a `useEffect` (rather than the render body) keeps the
  // render pure and is safe under React 18 concurrent rendering.
  const [editingGoalCount, setEditingGoalCount] = useState(0);
  const editingGoalCountRef = useRef(0);
  const editingSettingsRef = useRef(false);
  const addingGoalRef = useRef(false);
  useEffect(() => {
    editingGoalCountRef.current = editingGoalCount;
  }, [editingGoalCount]);
  useEffect(() => {
    editingSettingsRef.current = editingSettings;
  }, [editingSettings]);
  useEffect(() => {
    addingGoalRef.current = addingGoal;
  }, [addingGoal]);

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;

    function applyDept(d: DepartmentState, isInitial: boolean) {
      if (cancelled) return;
      setDept(d);
      // Don't replace `goals` while any row is being edited — the row
      // owns its form state and a server snapshot would flicker the
      // visible label. Initial load always wins (the user hasn't had
      // a chance to start editing yet).
      if (isInitial || editingGoalCountRef.current === 0) {
        setGoals(d.goals);
      }
      // Same guard for the big settings form.
      if (isInitial || !editingSettingsRef.current) {
        setSettingsForm({
          authority_level: d.config.authority_level,
          mission: d.config.charter.mission,
          cadences: { ...d.config.cadences },
          headcount: d.headcount != null ? String(d.headcount) : "",
          budget_usd: d.budget_usd != null ? String(d.budget_usd) : "",
          head_person_id: d.config.head_person_id,
          slack_channel_id: d.config.slack_channel_id ?? "",
          discord_channel_id: d.config.discord_channel_id ?? "",
          telegram_chat_id: d.config.telegram_chat_id ?? "",
          watched_entities: (d.config.watched_entities ?? []).join("\n"),
        });
      }
      if (isInitial && d.config.head_person_id != null) {
        listPeople().then(setPeople).catch(() => {});
      }
    }

    getDepartment(slug)
      .then((d) => applyDept(d, /* isInitial */ true))
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    // Poll for cross-tab edits and (Phase A's whole point) status
    // updates from the department_check_in workflow firing in the
    // background. Pause while any inline form is open.
    const interval = window.setInterval(() => {
      if (
        editingGoalCountRef.current > 0
        || editingSettingsRef.current
        || addingGoalRef.current
      ) {
        return;
      }
      getDepartment(slug)
        .then((d) => applyDept(d, /* isInitial */ false))
        .catch(() => {
          // Swallow transient errors — the next tick retries.
        });
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [slug]);

  // Load people whenever the user opens edit mode (if not already loaded).
  useEffect(() => {
    if (editingSettings && people.length === 0) {
      listPeople().then(setPeople).catch(() => {});
    }
  }, [editingSettings]); // eslint-disable-line react-hooks/exhaustive-deps

  async function saveSettings() {
    if (!dept) return;
    setSavingSettings(true);
    setSettingsErr(null);
    try {
      const headcountNum = settingsForm.headcount.trim() !== "" ? Number(settingsForm.headcount) : undefined;
      const budgetNum = settingsForm.budget_usd.trim() !== "" ? Number(settingsForm.budget_usd) : undefined;
      const updated = await updateDepartment(slug, {
        authority_level: settingsForm.authority_level,
        charter: {
          mission: settingsForm.mission,
          scope: dept.config.charter.scope,
          out_of_scope: dept.config.charter.out_of_scope,
        },
        cadences: settingsForm.cadences,
        headcount: headcountNum,
        budget_usd: budgetNum,
        head_person_id: settingsForm.head_person_id,
        // Empty string → null clears the channel; non-empty trim sends the new id.
        slack_channel_id: settingsForm.slack_channel_id.trim() || null,
        discord_channel_id: settingsForm.discord_channel_id.trim() || null,
        telegram_chat_id: settingsForm.telegram_chat_id.trim() || null,
        // Always sent: an emptied textarea clears the list.
        watched_entities: settingsForm.watched_entities
          .split("\n")
          .map((line) => line.trim())
          .filter((line) => line.length > 0),
      });
      setDept(updated);
      setEditingSettings(false);
    } catch (e) {
      setSettingsErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSavingSettings(false);
    }
  }

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6">
          {loading && <p className="text-fg-muted text-sm">Loading…</p>}
          {error && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
              {error}
            </div>
          )}
          {dept && (
            <>
              {/* Header */}
              <div className="flex items-start justify-between mb-6 gap-3">
                <div>
                  <h1 className="text-xl font-semibold text-fg">{dept.config.title}</h1>
                  <div className="text-xs text-fg-muted mt-1">
                    {dept.config.specialist_key ? (
                      <>Specialist: <code className="font-mono text-fg">{dept.config.specialist_key}</code></>
                    ) : (
                      <span className="italic">Informational department (no specialist agent)</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    onClick={() => {
                      if (editingSettings) {
                        // reset
                        setSettingsForm({
                          authority_level: dept.config.authority_level,
                          mission: dept.config.charter.mission,
                          cadences: { ...dept.config.cadences },
                          headcount: dept.headcount != null ? String(dept.headcount) : "",
                          budget_usd: dept.budget_usd != null ? String(dept.budget_usd) : "",
                          head_person_id: dept.config.head_person_id,
                          slack_channel_id: dept.config.slack_channel_id ?? "",
                          discord_channel_id: dept.config.discord_channel_id ?? "",
                          telegram_chat_id: dept.config.telegram_chat_id ?? "",
                          watched_entities: (dept.config.watched_entities ?? []).join("\n"),
                        });
                        setSettingsErr(null);
                      }
                      setEditingSettings((v) => !v);
                    }}
                    className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay transition-colors"
                  >
                    {editingSettings ? "Cancel" : "Edit settings"}
                  </button>
                  <button
                    disabled={deleting}
                    onClick={async () => {
                      if (!window.confirm(
                        `Delete "${dept.config.title}"? This will also remove all its Goals and cannot be undone.`
                      )) return;
                      setDeleting(true);
                      setDeleteErr(null);
                      try {
                        await deleteDepartment(slug);
                        router.push("/departments");
                      } catch (e) {
                        setDeleteErr(e instanceof Error ? e.message : "Delete failed");
                        setDeleting(false);
                      }
                    }}
                    className="px-3 py-1.5 text-xs rounded-lg bg-rose-500/10 hover:bg-rose-500/20 border border-rose-500/30 text-rose-300 disabled:opacity-50 transition-colors"
                  >
                    {deleting ? "Deleting…" : "Delete department"}
                  </button>
                </div>
              </div>
              {deleteErr && (
                <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm mb-4">
                  {deleteErr}
                </div>
              )}

              {/* Settings — three stacked cards in edit mode, single summary card in read mode */}
              {editingSettings ? (
                <div className="space-y-4 mb-6">
                  {/* Card 1: Charter */}
                  <section className="rounded-xl border border-line bg-surface-elevated px-4 py-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-3">Charter</h3>
                    <label className="text-xs text-fg-muted flex flex-col gap-1">
                      Mission
                      <textarea
                        value={settingsForm.mission}
                        onChange={(e) =>
                          setSettingsForm((f) => ({ ...f, mission: e.target.value }))
                        }
                        rows={3}
                        className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500 resize-none"
                      />
                    </label>
                  </section>

                  {/* Card 2: How it acts */}
                  <section className="rounded-xl border border-line bg-surface-elevated px-4 py-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-3">How it acts</h3>
                    <div className="space-y-3">
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Department head
                        <select
                          value={settingsForm.head_person_id ?? ""}
                          onChange={(e) =>
                            setSettingsForm((f) => ({
                              ...f,
                              head_person_id: e.target.value ? Number(e.target.value) : null,
                            }))
                          }
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        >
                          <option value="">— None —</option>
                          {people.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.full_name}{p.role ? ` — ${p.role}` : ""}{p.is_principal ? " (you)" : ""}
                            </option>
                          ))}
                        </select>
                        <span className="text-[10px] text-fg-muted">
                          The Executive surfaces this person as the department owner in routing decisions.
                        </span>
                      </label>
                      <div className="space-y-1.5">
                        {AUTHORITY_OPTS.map((a) => {
                          const meta = AUTHORITY_META[a];
                          const checked = settingsForm.authority_level === a;
                          return (
                            <label
                              key={a}
                              className={cls(
                                "flex items-start gap-2.5 p-2.5 rounded-lg border cursor-pointer transition-colors",
                                checked
                                  ? "border-indigo-500/50 bg-indigo-600/10"
                                  : "border-line hover:border-indigo-500/30 bg-surface-input"
                              )}
                            >
                              <input
                                type="radio"
                                name="authority_level"
                                value={a}
                                checked={checked}
                                onChange={() =>
                                  setSettingsForm((f) => ({ ...f, authority_level: a }))
                                }
                                className="mt-0.5 accent-indigo-500 flex-shrink-0"
                              />
                              <div className="min-w-0">
                                <div className="text-xs font-medium text-fg">{meta.label}</div>
                                <div className="text-[10px] text-fg-muted mt-0.5">{meta.hint}</div>
                              </div>
                            </label>
                          );
                        })}
                      </div>

                      <div>
                        <div className="text-xs text-fg-muted mb-1">Recurring check-in</div>
                        {Object.entries(settingsForm.cadences).map(([name, spec]) => (
                          <div key={name} className="flex items-center gap-2 mb-1.5">
                            <input
                              value={name}
                              readOnly
                              className="flex-1 px-2 py-1.5 rounded-lg bg-surface-input border border-line text-xs font-mono text-fg-muted focus:outline-none"
                            />
                            <input
                              value={spec}
                              onChange={(e) =>
                                setSettingsForm((f) => ({
                                  ...f,
                                  cadences: { ...f.cadences, [name]: e.target.value },
                                }))
                              }
                              className="flex-1 px-2 py-1.5 rounded-lg bg-surface-input border border-line text-xs font-mono focus:outline-none focus:border-indigo-500"
                              placeholder="daily@09:00"
                            />
                          </div>
                        ))}
                        <p className="text-[10px] text-fg-muted mt-1">
                          When set, the specialist posts a check-in on this schedule. You'll see it in Today.
                          Examples (UTC): <code className="font-mono">daily@09:00</code>, <code className="font-mono">weekly@mon@09:00</code>, <code className="font-mono">quarterly@01-09:00</code>.
                        </p>
                      </div>
                    </div>
                  </section>

                  {/* Card 3: Numbers */}
                  <section className="rounded-xl border border-line bg-surface-elevated px-4 py-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-3">Numbers</h3>
                    <div className="grid grid-cols-2 gap-2">
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Headcount
                        <input
                          type="number"
                          min={0}
                          value={settingsForm.headcount}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, headcount: e.target.value }))
                          }
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Budget (USD)
                        <input
                          type="number"
                          min={0}
                          value={settingsForm.budget_usd}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, budget_usd: e.target.value }))
                          }
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                    </div>
                  </section>

                  {/* Card 4: Broadcast channels — OE can post to these team rooms */}
                  <section className="rounded-xl border border-line bg-surface-elevated px-4 py-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-1">Team channels</h3>
                    <p className="text-[10px] text-fg-muted mb-3">
                      When set, the Executive can post department-scoped updates to these rooms via{" "}
                      <code className="font-mono">send_department_message</code>. Leave blank to have OE
                      fall back to DMing the department head.
                    </p>
                    <div className="space-y-2">
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Slack channel ID
                        <input
                          type="text"
                          value={settingsForm.slack_channel_id}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, slack_channel_id: e.target.value }))
                          }
                          placeholder="C01234ABCDE"
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm font-mono focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Discord channel ID
                        <input
                          type="text"
                          value={settingsForm.discord_channel_id}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, discord_channel_id: e.target.value }))
                          }
                          placeholder="123456789012345678"
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm font-mono focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Telegram chat ID
                        <input
                          type="text"
                          value={settingsForm.telegram_chat_id}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, telegram_chat_id: e.target.value }))
                          }
                          placeholder="-1001234567890"
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm font-mono focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                    </div>
                  </section>

                  {/* Card 5: Watched entities — strong grounding for the research watch policy */}
                  <section className="rounded-xl border border-line bg-surface-elevated px-4 py-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-1">Watched entities</h3>
                    <p className="text-[10px] text-fg-muted mb-3">
                      Vendors, competitors or tickers this department cares about, one per line. Named here,
                      the Executive will start watching their status pages, filings and feeds on its own and
                      route what it finds to this department and its head.
                    </p>
                    <label className="text-xs text-fg-muted flex flex-col gap-1">
                      Watched entities (one per line)
                      <textarea
                        value={settingsForm.watched_entities}
                        onChange={(e) =>
                          setSettingsForm((f) => ({ ...f, watched_entities: e.target.value }))
                        }
                        rows={4}
                        placeholder={"Brex\nStripe\nACME"}
                        className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                      />
                    </label>
                  </section>

                  {settingsErr && <p className="text-xs text-rose-300">{settingsErr}</p>}
                  <button
                    disabled={savingSettings}
                    onClick={saveSettings}
                    className="px-4 py-1.5 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50"
                  >
                    {savingSettings ? "Saving…" : "Save settings"}
                  </button>
                </div>
              ) : (
                <section className="rounded-xl border border-line bg-surface-elevated px-4 py-4 mb-6">
                  <div className="divide-y divide-line">
                    <div className="flex items-start gap-3 py-2">
                      <div className="w-36 flex-shrink-0 text-xs text-fg-muted pt-0.5">How it acts</div>
                      <div>
                        <div className="text-sm text-fg">
                          {AUTHORITY_META[dept.config.authority_level].label}
                        </div>
                        <div className="text-[10px] text-fg-muted mt-0.5">
                          {AUTHORITY_META[dept.config.authority_level].hint}
                        </div>
                      </div>
                    </div>
                    {[
                      ["Mission", dept.config.charter.mission || "—"],
                      ...(dept.config.head_person_id != null
                        ? [["Head", people.find((p) => p.id === dept.config.head_person_id)?.full_name ?? `Person #${dept.config.head_person_id}`]]
                        : []),
                      ...(dept.headcount != null ? [["Headcount", String(dept.headcount)]] : []),
                      ...(dept.budget_usd != null ? [["Budget", `$${dept.budget_usd.toLocaleString()}`]] : []),
                      ...(dept.config.slack_channel_id ? [["Slack channel", dept.config.slack_channel_id]] : []),
                      ...(dept.config.discord_channel_id ? [["Discord channel", dept.config.discord_channel_id]] : []),
                      ...(dept.config.telegram_chat_id ? [["Telegram chat", dept.config.telegram_chat_id]] : []),
                      ...((dept.config.watched_entities ?? []).length > 0
                        ? [["Watched entities", (dept.config.watched_entities ?? []).join(", ")]]
                        : []),
                    ].map(([label, value]) => (
                      <div key={label} className="flex items-start gap-3 py-2">
                        <div className="w-36 flex-shrink-0 text-xs text-fg-muted pt-0.5">{label}</div>
                        <div className="text-sm text-fg">{value}</div>
                      </div>
                    ))}
                    {Object.entries(dept.config.cadences).length > 0 && (
                      <div className="flex items-start gap-3 py-2">
                        <div className="w-36 flex-shrink-0 text-xs text-fg-muted pt-0.5">Recurring check-in</div>
                        <div className="flex flex-wrap gap-1.5">
                          {Object.entries(dept.config.cadences).map(([n, s]) => (
                            <span key={n} className="px-2 py-0.5 rounded bg-surface-overlay border border-line text-xs font-mono text-fg">
                              {n}: {s}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </section>
              )}

              {/* Charter scope (read-only) */}
              {(dept.config.charter.scope.length > 0 || dept.config.charter.out_of_scope.length > 0) && (
                <section className="mb-6 grid grid-cols-1 sm:grid-cols-2 gap-4">
                  {dept.config.charter.scope.length > 0 && (
                    <div>
                      <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-2">In Scope</h2>
                      <ul className="list-disc list-inside space-y-1">
                        {dept.config.charter.scope.map((s, i) => (
                          <li key={i} className="text-sm text-fg">{s}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {dept.config.charter.out_of_scope.length > 0 && (
                    <div>
                      <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-2">Out of Scope</h2>
                      <ul className="list-disc list-inside space-y-1">
                        {dept.config.charter.out_of_scope.map((s, i) => (
                          <li key={i} className="text-sm text-fg-muted">{s}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </section>
              )}

              {/* Goals */}
              <section>
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
                    Goals ({goals.length})
                  </h2>
                  {!addingGoal && (
                    <button
                      onClick={() => setAddingGoal(true)}
                      className="px-3 py-1 text-xs rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30"
                    >
                      + Add Goal
                    </button>
                  )}
                </div>

                <div className="rounded-xl border border-line bg-surface-elevated px-4">
                  {addingGoal && (
                    <AddGoalForm
                      slug={slug}
                      onCreated={(goal) => {
                        setGoals((prev) => [...prev, goal]);
                        setAddingGoal(false);
                      }}
                      onCancel={() => setAddingGoal(false)}
                    />
                  )}
                  {goals.length === 0 && !addingGoal ? (
                    <p className="py-6 text-sm text-fg-muted text-center">
                      No Goals yet.{" "}
                      <button
                        onClick={() => setAddingGoal(true)}
                        className="text-indigo-400 hover:underline"
                      >
                        Add one →
                      </button>
                    </p>
                  ) : (
                    goals.map((goal) => (
                      <GoalRow
                        key={goal.id}
                        slug={slug}
                        goal={goal}
                        onSaved={(updated) =>
                          setGoals((prev) => prev.map((g) => (g.id === updated.id ? updated : g)))
                        }
                        onDeleted={(id) => setGoals((prev) => prev.filter((g) => g.id !== id))}
                        onEditingChange={(editing) =>
                          setEditingGoalCount((c) => Math.max(0, c + (editing ? 1 : -1)))
                        }
                      />
                    ))
                  )}
                </div>
              </section>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
