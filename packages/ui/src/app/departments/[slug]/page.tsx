"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";

import { AddGoalForm, GoalRow } from "@/components/goals/GoalEditor";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import SidePanel from "@/components/ui/SidePanel";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  deleteDepartment,
  getDepartment,
  listPeople,
  updateDepartment,
  type DepartmentConfig,
  type DepartmentDetail,
  type Goal,
  type Person,
} from "@/lib/api";
import {
  WEEKDAYS,
  checkInSpec,
  describeCheckIn,
  formatNextCheckIn,
  parseCheckIn,
  withKind,
  type CheckInChoice,
  type Weekday,
} from "@/lib/checkInCadence";

// Auto-refresh cadence for the detail page. The `dept_cadence` scheduler
// fires at most once per department per cadence (default weekly), so any
// poll faster than ~30s is overkill for review-driven status changes
// but keeps the page feeling live for cross-tab edits.
const POLL_INTERVAL_MS = 30_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

// The settings form, one short section at a time.
type SettingsTab = "charter" | "acts" | "numbers" | "channels" | "watched";
const SETTINGS_TABS: { id: SettingsTab; label: string }[] = [
  { id: "charter", label: "Charter" },
  { id: "acts", label: "How it acts" },
  { id: "numbers", label: "Numbers" },
  { id: "channels", label: "Channels" },
  { id: "watched", label: "Watched" },
];

const INPUT_CLS =
  "w-full px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";
const FIELD_CLS = `${INPUT_CLS} h-11`;
const LABEL_CLS = "text-sm text-fg-muted flex flex-col gap-1.5";
const HINT_CLS = "text-[13px] text-fg-subtle";
const SECTION_INTRO_CLS = "text-[15px] text-fg-muted mb-4";

const CHECK_IN_KINDS: { id: "daily" | "weekly" | "off"; label: string }[] = [
  { id: "daily", label: "Daily" },
  { id: "weekly", label: "Weekly" },
  { id: "off", label: "Off" },
];

/**
 * Daily / Weekly (a day and a time) / Off for the department's check-in.
 * A spec the picker can't show (a quarterly one) stays as it is until one of
 * the three is picked.
 */
function CheckInPicker({
  value,
  onChange,
}: {
  value: CheckInChoice;
  onChange: (next: CheckInChoice) => void;
}) {
  const segCls = (active: boolean) =>
    cls(
      "flex-1 h-11 px-4 text-[15px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60",
      active ? "bg-accent/15 text-fg" : "text-fg-muted hover:text-fg hover:bg-surface-overlay",
    );
  return (
    <fieldset className="space-y-3">
      <legend className="text-sm text-fg-muted mb-1.5">Recurring check-in</legend>
      <div role="radiogroup" aria-label="How often it checks in" className="flex rounded-xl border border-line overflow-hidden divide-x divide-line">
        {CHECK_IN_KINDS.map((k) => (
          <button
            key={k.id}
            type="button"
            role="radio"
            aria-checked={value.kind === k.id}
            onClick={() => onChange(withKind(value, k.id))}
            className={segCls(value.kind === k.id)}
          >
            {k.label}
          </button>
        ))}
      </div>
      {value.kind === "custom" && (
        <p className={HINT_CLS}>
          Currently <code className="font-mono">{value.spec}</code>. Pick Daily, Weekly or Off to change it.
        </p>
      )}
      {(value.kind === "daily" || value.kind === "weekly") && (
        <div className="flex flex-wrap gap-3">
          {value.kind === "weekly" && (
            <label className={cls(LABEL_CLS, "flex-1 min-w-[10rem]")}>
              Day
              <select
                value={value.day}
                onChange={(e) => onChange({ ...value, day: e.target.value as Weekday })}
                className={FIELD_CLS}
              >
                {WEEKDAYS.map((d) => (
                  <option key={d.id} value={d.id}>{d.label}</option>
                ))}
              </select>
            </label>
          )}
          <label className={cls(LABEL_CLS, "flex-1 min-w-[10rem]")}>
            Time (UTC)
            <input
              type="time"
              step={60}
              required
              value={value.time}
              onChange={(e) => {
                if (e.target.value) onChange({ ...value, time: e.target.value.slice(0, 5) });
              }}
              className={FIELD_CLS}
            />
          </label>
        </div>
      )}
      <p className={HINT_CLS}>
        {value.kind === "off"
          ? "No scheduled check-ins. The specialist still answers when you ask."
          : "The specialist reviews this department\u2019s goals on this schedule. You\u2019ll see it on Home."}
      </p>
    </fieldset>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function DepartmentDetailPage() {
  const params = useParams();
  const slug = params?.slug as string;
  const router = useRouter();
  const { mode, effectiveTimezone } = useWorkspace();

  const [dept, setDept] = useState<DepartmentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // People for the head-person picker (loaded lazily)
  const [people, setPeople] = useState<Person[]>([]);

  // Settings edit state
  const [editingSettings, setEditingSettings] = useState(false);
  const [settingsForm, setSettingsForm] = useState({
    authority_level: "propose_only" as DepartmentConfig["authority_level"],
    mission: "",
    checkIn: { kind: "off" } as CheckInChoice,
    headcount: "",
    budget_usd: "",
    head_person_id: null as number | null,
    slack_channel_id: "",
    discord_channel_id: "",
    telegram_chat_id: "",
    // One entity per line in the textarea; split + trimmed on save.
    watched_entities: "",
  });
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("charter");
  const settingsTabsId = useId();
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

    function applyDept(d: DepartmentDetail, isInitial: boolean) {
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
          checkIn: parseCheckIn(d.config.cadences.check_in),
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

  function openSettings() {
    setSettingsTab("charter");
    setEditingSettings(true);
  }

  // Close the settings panel, dropping unsaved changes.
  function cancelSettings() {
    if (!dept || savingSettings) return;
    setSettingsForm({
      authority_level: dept.config.authority_level,
      mission: dept.config.charter.mission,
      checkIn: parseCheckIn(dept.config.cadences.check_in),
      headcount: dept.headcount != null ? String(dept.headcount) : "",
      budget_usd: dept.budget_usd != null ? String(dept.budget_usd) : "",
      head_person_id: dept.config.head_person_id,
      slack_channel_id: dept.config.slack_channel_id ?? "",
      discord_channel_id: dept.config.discord_channel_id ?? "",
      telegram_chat_id: dept.config.telegram_chat_id ?? "",
      watched_entities: (dept.config.watched_entities ?? []).join("\n"),
    });
    setSettingsErr(null);
    setEditingSettings(false);
  }

  async function removeDepartment() {
    if (!dept) return;
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
  }

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
        // The other named cadences ride along unchanged; "Off" is an empty spec.
        cadences: { ...dept.config.cadences, check_in: checkInSpec(settingsForm.checkIn) },
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
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px]">
              {error}
            </div>
          )}
          {dept && (
            <>
              {/* Header */}
              <div className="flex items-start justify-between mb-6 gap-4">
                <div className="min-w-0">
                  <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">{dept.config.title}</h1>
                  <div className="text-[15px] text-fg-muted mt-1.5">
                    {dept.config.specialist_key ? (
                      <>Specialist: <code className="font-mono text-sm text-fg">{dept.config.specialist_key}</code></>
                    ) : (
                      <span className="italic">Informational department (no specialist agent)</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button onClick={openSettings}>Edit settings</Button>
                  <OverflowMenu
                    label={`More actions for ${dept.config.title}`}
                    items={[
                      {
                        label: deleting ? "Deleting…" : "Delete department",
                        danger: true,
                        disabled: deleting,
                        onSelect: () => void removeDepartment(),
                      },
                    ]}
                  />
                </div>
              </div>
              {deleteErr && (
                <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
                  {deleteErr}
                </div>
              )}

              {/* Settings summary; Edit settings opens the form in a side panel */}
              <section className="rounded-2xl border border-line bg-surface-elevated px-5 py-2 mb-8">
                <dl className="divide-y divide-line">
                  <div className="flex flex-col sm:flex-row sm:items-baseline gap-0.5 sm:gap-4 py-3">
                    <dt className="sm:w-40 flex-shrink-0 text-sm text-fg-muted">How it acts</dt>
                    <dd className="min-w-0">
                      <div className="text-[15px] text-fg">
                        {AUTHORITY_META[dept.config.authority_level].label}
                      </div>
                      <div className="text-sm text-fg-muted mt-0.5">
                        {AUTHORITY_META[dept.config.authority_level].hint}
                      </div>
                    </dd>
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
                    <div key={label} className="flex flex-col sm:flex-row sm:items-baseline gap-0.5 sm:gap-4 py-3">
                      <dt className="sm:w-40 flex-shrink-0 text-sm text-fg-muted">{label}</dt>
                      <dd className="text-[15px] text-fg min-w-0 break-words">{value}</dd>
                    </div>
                  ))}
                  <div className="flex flex-col sm:flex-row sm:items-baseline gap-0.5 sm:gap-4 py-3">
                    <dt className="sm:w-40 flex-shrink-0 text-sm text-fg-muted">Recurring check-in</dt>
                    <dd className="min-w-0">
                      <div className="text-[15px] text-fg">
                        {describeCheckIn(parseCheckIn(dept.config.cadences.check_in))}
                      </div>
                      {mode === "solo" ? (
                        <div className="text-sm text-fg-muted mt-0.5">
                          Paused while you use Open Executive just for yourself.
                        </div>
                      ) : (
                        formatNextCheckIn(dept.next_check_in_at, effectiveTimezone) && (
                          <div className="text-sm text-fg-muted mt-0.5">
                            Next: {formatNextCheckIn(dept.next_check_in_at, effectiveTimezone)}
                          </div>
                        )
                      )}
                    </dd>
                  </div>
                </dl>
              </section>

              {/* Charter scope (read-only) */}
              {(dept.config.charter.scope.length > 0 || dept.config.charter.out_of_scope.length > 0) && (
                <section className="mb-8 grid grid-cols-1 sm:grid-cols-2 gap-6">
                  {dept.config.charter.scope.length > 0 && (
                    <div>
                      <h2 className="text-lg font-semibold text-fg mb-2">In scope</h2>
                      <ul className="list-disc pl-5 space-y-1.5">
                        {dept.config.charter.scope.map((s, i) => (
                          <li key={i} className="text-[15px] text-fg">{s}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {dept.config.charter.out_of_scope.length > 0 && (
                    <div>
                      <h2 className="text-lg font-semibold text-fg mb-2">Out of scope</h2>
                      <ul className="list-disc pl-5 space-y-1.5">
                        {dept.config.charter.out_of_scope.map((s, i) => (
                          <li key={i} className="text-[15px] text-fg-muted">{s}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </section>
              )}

              {/* Goals */}
              <section>
                <div className="flex items-center justify-between gap-3 mb-3">
                  <h2 className="text-lg font-semibold text-fg">
                    Goals <span className="font-normal text-fg-subtle">{goals.length}</span>
                  </h2>
                  {!addingGoal && (
                    <Button variant="primary" onClick={() => setAddingGoal(true)}>
                      Add goal
                    </Button>
                  )}
                </div>

                <div className="rounded-2xl border border-line bg-surface-elevated px-5">
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
                    <p className="py-8 text-[15px] text-fg-muted text-center">
                      No Goals yet. Add the first one with Add goal.
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

              <SidePanel
                open={editingSettings}
                onClose={cancelSettings}
                title="Department settings"
                subtitle={dept.config.title}
                width="lg"
                footer={
                  <div className="space-y-2">
                    {settingsErr && <p className="text-sm text-rose-500">{settingsErr}</p>}
                    <div className="flex gap-2">
                      <Button variant="primary" disabled={savingSettings} onClick={saveSettings} className="flex-1 sm:flex-none">
                        {savingSettings ? "Saving…" : "Save settings"}
                      </Button>
                      <Button disabled={savingSettings} onClick={cancelSettings}>
                        Cancel
                      </Button>
                    </div>
                  </div>
                }
              >
                <div className="mb-5">
                  <SectionTabs
                    idBase={settingsTabsId}
                    label="Settings sections"
                    tabs={SETTINGS_TABS}
                    active={settingsTab}
                    onChange={setSettingsTab}
                  />
                </div>
                <div {...sectionPanelProps(settingsTabsId, settingsTab)}>
                  {settingsTab === "charter" && (
                    <label className={LABEL_CLS}>
                      Mission
                      <textarea
                        value={settingsForm.mission}
                        onChange={(e) =>
                          setSettingsForm((f) => ({ ...f, mission: e.target.value }))
                        }
                        rows={5}
                        className={`${INPUT_CLS} py-2.5 resize-none`}
                      />
                    </label>
                  )}

                  {settingsTab === "acts" && (
                    <div className="space-y-6">
                      <label className={LABEL_CLS}>
                        Department head
                        <select
                          value={settingsForm.head_person_id ?? ""}
                          onChange={(e) =>
                            setSettingsForm((f) => ({
                              ...f,
                              head_person_id: e.target.value ? Number(e.target.value) : null,
                            }))
                          }
                          className={FIELD_CLS}
                        >
                          <option value="">— None —</option>
                          {people.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.full_name}{p.role ? ` — ${p.role}` : ""}{p.is_principal ? " (you)" : ""}
                            </option>
                          ))}
                        </select>
                        <span className={HINT_CLS}>
                          The Executive surfaces this person as the department owner in routing decisions.
                        </span>
                      </label>
                      <fieldset className="space-y-2">
                        <legend className="text-sm text-fg-muted mb-1.5">How it acts</legend>
                        {AUTHORITY_OPTS.map((a) => {
                          const meta = AUTHORITY_META[a];
                          const checked = settingsForm.authority_level === a;
                          return (
                            <label
                              key={a}
                              className={cls(
                                "flex items-start gap-3 p-3.5 rounded-xl border cursor-pointer transition-colors",
                                checked
                                  ? "border-accent/60 bg-accent/10"
                                  : "border-line hover:border-line-strong bg-surface-elevated"
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
                                className="mt-1 w-4 h-4 accent-indigo-500 flex-shrink-0"
                              />
                              <div className="min-w-0">
                                <div className="text-[15px] font-medium text-fg">{meta.label}</div>
                                <div className="text-sm text-fg-muted mt-0.5">{meta.hint}</div>
                              </div>
                            </label>
                          );
                        })}
                      </fieldset>

                      <CheckInPicker
                        value={settingsForm.checkIn}
                        onChange={(checkIn) => setSettingsForm((f) => ({ ...f, checkIn }))}
                      />
                    </div>
                  )}

                  {settingsTab === "numbers" && (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <label className={LABEL_CLS}>
                        Headcount
                        <input
                          type="number"
                          min={0}
                          value={settingsForm.headcount}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, headcount: e.target.value }))
                          }
                          className={FIELD_CLS}
                        />
                      </label>
                      <label className={LABEL_CLS}>
                        Budget (USD)
                        <input
                          type="number"
                          min={0}
                          value={settingsForm.budget_usd}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, budget_usd: e.target.value }))
                          }
                          className={FIELD_CLS}
                        />
                      </label>
                    </div>
                  )}

                  {/* Broadcast channels — OE can post to these team rooms */}
                  {settingsTab === "channels" && (
                    <>
                      <p className={SECTION_INTRO_CLS}>
                        When set, the Executive can post department-scoped updates to these rooms via{" "}
                        <code className="font-mono text-sm">send_department_message</code>. Leave blank to have OE
                        fall back to DMing the department head.
                      </p>
                      <div className="space-y-4">
                        <label className={LABEL_CLS}>
                          Slack channel ID
                          <input
                            type="text"
                            value={settingsForm.slack_channel_id}
                            onChange={(e) =>
                              setSettingsForm((f) => ({ ...f, slack_channel_id: e.target.value }))
                            }
                            placeholder="C01234ABCDE"
                            className={`${FIELD_CLS} font-mono`}
                          />
                        </label>
                        <label className={LABEL_CLS}>
                          Discord channel ID
                          <input
                            type="text"
                            value={settingsForm.discord_channel_id}
                            onChange={(e) =>
                              setSettingsForm((f) => ({ ...f, discord_channel_id: e.target.value }))
                            }
                            placeholder="123456789012345678"
                            className={`${FIELD_CLS} font-mono`}
                          />
                        </label>
                        <label className={LABEL_CLS}>
                          Telegram chat ID
                          <input
                            type="text"
                            value={settingsForm.telegram_chat_id}
                            onChange={(e) =>
                              setSettingsForm((f) => ({ ...f, telegram_chat_id: e.target.value }))
                            }
                            placeholder="-1001234567890"
                            className={`${FIELD_CLS} font-mono`}
                          />
                        </label>
                      </div>
                    </>
                  )}

                  {/* Watched entities — strong grounding for the research watch policy */}
                  {settingsTab === "watched" && (
                    <>
                      <p className={SECTION_INTRO_CLS}>
                        Vendors, competitors or tickers this department cares about, one per line. Named here,
                        the Executive will start watching their status pages, filings and feeds on its own and
                        route what it finds to this department and its head.
                      </p>
                      <label className={LABEL_CLS}>
                        Watched entities (one per line)
                        <textarea
                          value={settingsForm.watched_entities}
                          onChange={(e) =>
                            setSettingsForm((f) => ({ ...f, watched_entities: e.target.value }))
                          }
                          rows={6}
                          placeholder={"Brex\nStripe\nACME"}
                          className={`${INPUT_CLS} py-2.5`}
                        />
                      </label>
                    </>
                  )}
                </div>
              </SidePanel>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
