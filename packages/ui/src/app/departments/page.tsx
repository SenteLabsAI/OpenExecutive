"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { GoalStatusPill } from "@/components/goals/GoalEditor";
import Button from "@/components/ui/Button";
import SidePanel from "@/components/ui/SidePanel";
import {
  createDepartment,
  listDepartments,
  type DepartmentCreate,
  type DepartmentState,
} from "@/lib/api";

const AUTHORITY_LABELS: Record<string, string> = {
  auto_execute: "Auto",
  propose_only: "Propose",
  escalate: "Escalate",
};

const AUTHORITY_DOT: Record<string, string> = {
  auto_execute: "bg-emerald-500",
  propose_only: "bg-sky-500",
  escalate: "bg-amber-500",
};

const INPUT_CLS =
  "w-full px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";

interface AddDepartmentModalProps {
  onCreated: (dept: DepartmentState) => void;
  onCancel: () => void;
}

function AddDepartmentModal({ onCreated, onCancel }: AddDepartmentModalProps) {
  const [form, setForm] = useState<DepartmentCreate>({ title: "", mission: "" });
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // After the panel has taken focus, so closing it returns focus to the opener.
    const id = requestAnimationFrame(() => titleRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, []);

  async function handleCreate() {
    setSaving(true);
    setErr(null);
    try {
      const dept = await createDepartment({ title: form.title.trim(), mission: form.mission });
      onCreated(dept);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <SidePanel
      open
      onClose={saving ? () => {} : onCancel}
      title="New department"
      footer={
        <div className="flex gap-2">
          <Button variant="primary" disabled={saving || !form.title.trim()} onClick={handleCreate} className="flex-1">
            {saving ? "Creating…" : "Create"}
          </Button>
          <Button disabled={saving} onClick={onCancel}>
            Cancel
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        <label className="text-sm text-fg-muted flex flex-col gap-1.5">
          <span>Name <span className="text-rose-500">*</span></span>
          <input
            ref={titleRef}
            value={form.title}
            onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
            onKeyDown={(e) => { if (e.key === "Enter" && form.title.trim()) handleCreate(); }}
            className={`${INPUT_CLS} h-11`}
            placeholder="Customer Success"
          />
        </label>
        <label className="text-sm text-fg-muted flex flex-col gap-1.5">
          <span>Mission <span className="text-fg-subtle">(optional)</span></span>
          <textarea
            value={form.mission}
            onChange={(e) => setForm((f) => ({ ...f, mission: e.target.value }))}
            rows={4}
            className={`${INPUT_CLS} py-2.5 resize-none`}
            placeholder="What does this department own?"
          />
        </label>
        {err && <p className="text-sm text-rose-500">{err}</p>}
      </div>
    </SidePanel>
  );
}

export default function DepartmentsPage() {
  const [depts, setDepts] = useState<DepartmentState[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [addingDept, setAddingDept] = useState(false);

  useEffect(() => {
    listDepartments()
      .then(setDepts)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-8">
            <div className="min-w-0">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Departments</h1>
              <p className="text-[15px] text-fg-muted mt-2">
                Each department wraps a specialist agent with persistent Goals, authority level, and cadences.
              </p>
            </div>
            <Button variant="primary" onClick={() => setAddingDept(true)} className="flex-shrink-0 self-start">
              Add department
            </Button>
          </div>

          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
              {error}
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {depts.map((ds) => {
              const cfg = ds.config;
              const atRisk = ds.goals.filter((g) => g.status === "at_risk").length;
              const offTrack = ds.goals.filter((g) => g.status === "off_track").length;
              return (
                <Link
                  key={cfg.slug}
                  href={`/departments/${cfg.slug}`}
                  className="flex flex-col rounded-2xl border border-line bg-surface-elevated hover:bg-surface-hover hover:border-line-strong transition-colors p-5 group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="text-lg font-semibold text-fg group-hover:text-accent transition-colors min-w-0">
                      {cfg.title}
                    </div>
                    <span className="flex-shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-line text-[13px] font-medium text-fg">
                      <span aria-hidden="true" className={`h-2 w-2 rounded-full ${AUTHORITY_DOT[cfg.authority_level] ?? "bg-fg-subtle"}`} />
                      {AUTHORITY_LABELS[cfg.authority_level] ?? cfg.authority_level}
                    </span>
                  </div>
                  <div className="text-[15px] text-fg-muted mt-1.5">{cfg.charter.mission.slice(0, 80)}{cfg.charter.mission.length > 80 ? "…" : ""}</div>

                  <div className="flex flex-wrap items-center gap-2 mt-4 text-sm text-fg-muted">
                    <span className="mr-1">{ds.goals.length} Goal{ds.goals.length !== 1 ? "s" : ""}</span>
                    {atRisk > 0 && <GoalStatusPill status="at_risk" />}
                    {offTrack > 0 && <GoalStatusPill status="off_track" />}
                    {ds.goals.length > 0 && atRisk === 0 && offTrack === 0 && (
                      <GoalStatusPill status="on_track" />
                    )}
                  </div>
                </Link>
              );
            })}
          </div>
        </div>
      </main>

      {addingDept && (
        <AddDepartmentModal
          onCreated={(dept) => {
            setDepts((prev) => [...prev, dept]);
            setAddingDept(false);
          }}
          onCancel={() => setAddingDept(false)}
        />
      )}
    </div>
  );
}
