"use client";

import { useEffect, useMemo, useState } from "react";

import Switch from "@/components/Switch";
import RoleFields from "@/components/workspace/RoleFields";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  getDecisionClassMode,
  getPeopleViewer,
  getWorkspace,
  MEETING_SCHEDULING_CLASS,
  setDecisionClassMode,
  updateWorkspace,
  type DecisionClassMode,
  type WorkspaceMode,
} from "@/lib/api";
import { roleFormErrors, roleFormFrom, roleUpdate, type RoleForm } from "@/lib/principalRole";

// Settings → Workspace: who Open Executive is for (just you, or you and your
// team), your role when it's just you, the time zone its briefs run in, and
// whether it books meetings without asking, as one row each. Mode, role and
// zone go through PUT /workspace and then the app-wide WorkspaceProvider is
// refreshed so the nav and pages follow. The page supplies the section
// heading; this is the body.

const MODE_LABEL: Record<WorkspaceMode, string> = {
  solo: "Just me",
  team: "With your team",
};

// What changes, shown before the switch is made.
const SWITCH_EFFECT: Record<WorkspaceMode, string> = {
  team:
    "Departments and their daily check-ins come back, and the sidebar shows Departments and People again.",
  solo:
    "Department check-ins are paused and the sidebar shows your goals instead of departments. Nothing is deleted: your departments stay, as the areas your goals are grouped by, and switching back brings their check-ins back.",
};

function browserTimeZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

function allTimeZones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

export default function WorkspaceCard() {
  const { mode, timezone, effectiveTimezone, loading, refresh } = useWorkspace();
  const [pendingMode, setPendingMode] = useState<WorkspaceMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const browserZone = useMemo(browserTimeZone, []);
  const zones = useMemo(() => {
    const list = allTimeZones();
    // A stored zone the browser's list lacks (e.g. "UTC" in some engines)
    // must still show as selected.
    if (timezone && !list.includes(timezone)) list.unshift(timezone);
    return list;
  }, [timezone]);

  async function save(update: { mode?: WorkspaceMode; timezone?: string | null }) {
    setBusy(true);
    setError(null);
    try {
      await updateWorkspace(update);
      await refresh();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the change.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-md">
      <div className="divide-y divide-line">
        {/* Mode */}
        <div className="py-4 first:pt-0 last:pb-0">
          <div className="text-xs font-medium text-fg mb-1.5" id="ws-mode-label">
            Using Open Executive
          </div>
          <div role="radiogroup" aria-labelledby="ws-mode-label" className="inline-flex rounded-lg border border-line p-0.5 bg-surface">
            {(["solo", "team"] as const).map((m) => {
              const selected = mode === m;
              return (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  disabled={loading || busy}
                  onClick={() => {
                    setError(null);
                    setPendingMode(selected ? null : m);
                  }}
                  className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                    selected ? "bg-surface-overlay text-fg" : "text-fg-muted hover:text-fg"
                  }`}
                >
                  {MODE_LABEL[m]}
                </button>
              );
            })}
          </div>

          {pendingMode && pendingMode !== mode && (
            <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3">
              <p className="text-xs text-fg leading-relaxed">
                Switch to <span className="font-medium">{MODE_LABEL[pendingMode]}</span>?{" "}
                {SWITCH_EFFECT[pendingMode]}
              </p>
              <div className="mt-2.5 flex items-center gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={async () => {
                    if (await save({ mode: pendingMode })) setPendingMode(null);
                  }}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-500 hover:bg-indigo-600 text-white transition-colors cursor-pointer disabled:opacity-50"
                >
                  {busy ? "Switching…" : `Switch to ${MODE_LABEL[pendingMode]}`}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setPendingMode(null)}
                  className="px-3 py-1.5 rounded-lg text-xs text-fg-muted hover:text-fg transition-colors cursor-pointer disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>

        {mode === "solo" && <RoleSection />}

        {/* Time zone */}
        <div className="py-4 first:pt-0 last:pb-0">
          <label htmlFor="ws-timezone" className="text-xs font-medium text-fg">
            Time zone
          </label>
          <p className="text-xs text-fg-muted mt-0.5 mb-1.5">
            When your morning brief, end-of-day digest and reflection arrive, and how the
            Executive reads &ldquo;tomorrow at 9&rdquo;.
            {effectiveTimezone && <> Now: {effectiveTimezone}.</>}
          </p>
          <select
            id="ws-timezone"
            value={timezone ?? ""}
            disabled={loading || busy}
            onChange={(e) => void save({ timezone: e.target.value || null })}
            className="w-full px-2.5 py-1.5 rounded-lg text-sm bg-surface border border-line text-fg focus:outline-none focus:border-line-strong disabled:opacity-60"
          >
            <option value="">Follow server default</option>
            {browserZone && (
              <optgroup label="Suggested">
                <option value={browserZone}>{browserZone} (this browser)</option>
              </optgroup>
            )}
            <optgroup label="All time zones">
              {zones.map((z) => (
                <option key={z} value={z}>
                  {z}
                </option>
              ))}
            </optgroup>
          </select>
          {browserZone && timezone !== browserZone && (
            <button
              type="button"
              disabled={loading || busy}
              onClick={() => void save({ timezone: browserZone })}
              className="mt-1.5 text-xs text-indigo-400 hover:text-indigo-300 cursor-pointer disabled:opacity-50"
            >
              Use this browser&apos;s time zone ({browserZone})
            </button>
          )}
        </div>

        <CompanyDomainsSection />

        <MeetingAutonomySwitch />
      </div>

      {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
      <p className="mt-4 text-xs text-fg-subtle">
        A persona you customised in Council stays in place in either mode.
      </p>
    </div>
  );
}

// "Company email domains": addresses on these match a teammate by the part
// before the @ (anna+invoices@acme.io is the Anna at anna@acme.com); a new
// address there is pre-filled as a teammate when someone writes in. Derived
// from your own address unless set here. The server returns them only to the
// principal, so the row shows only to them.
function CompanyDomainsSection() {
  const [domains, setDomains] = useState<string[] | null>(null);
  const [custom, setCustom] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function apply(ws: { company_domains?: string[]; company_domains_custom?: boolean }) {
    const list = ws.company_domains ?? [];
    setDomains(list);
    setCustom(Boolean(ws.company_domains_custom));
    setDraft(list.join(", "));
  }

  useEffect(() => {
    const ctrl = new AbortController();
    getPeopleViewer()
      .then((viewer) => (viewer.is_principal ? getWorkspace(ctrl.signal).then(apply) : undefined))
      .catch(() => setDomains(null));
    return () => ctrl.abort();
  }, []);

  async function save(value: string[] | null) {
    setBusy(true);
    setError(null);
    try {
      apply(await updateWorkspace({ company_domains: value }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the domains");
    } finally {
      setBusy(false);
    }
  }

  if (domains === null) return null;
  const parsed = draft.split(/[\s,;]+/).map((d) => d.trim().toLowerCase()).filter(Boolean);
  return (
    <div className="py-4 first:pt-0 last:pb-0">
      <label htmlFor="ws-domains" className="text-xs font-medium text-fg">
        Company email domains
      </label>
      <p className="text-xs text-fg-muted mt-0.5 mb-1.5">
        Mail from these domains matches a teammate by the part before the @, so
        anna+invoices@ reaches the Anna already on your People list. Someone new
        writing from one is suggested as a teammate — you still confirm them.
        {!custom && " Taken from your own address until you set them."}
      </p>
      <div className="flex gap-2">
        <input
          id="ws-domains"
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="acme.com, acme.io"
          className="min-w-0 flex-1 px-2.5 py-1.5 rounded-lg text-sm bg-surface border border-line text-fg focus:outline-none focus:border-line-strong disabled:opacity-60"
        />
        <button
          type="button"
          disabled={busy || parsed.join(",") === domains.join(",")}
          onClick={() => void save(parsed.length ? parsed : null)}
          className="text-xs text-indigo-400 hover:text-indigo-300 cursor-pointer disabled:opacity-50"
        >
          Save
        </button>
        {custom && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void save(null)}
            className="text-xs text-fg-muted hover:text-fg cursor-pointer disabled:opacity-50"
          >
            Use my address
          </button>
        )}
      </div>
      {error && <p className="mt-1.5 text-xs text-red-400">{error}</p>}
    </div>
  );
}

// "Your role" (solo only): what kind of principal you are and what you do.
// The Executive and its specialists use it to fit their advice to your job.
// Edits stay local until saved; Save sends only the fields that changed.
function RoleSection() {
  const { role, loading, refresh } = useWorkspace();
  const [form, setForm] = useState<RoleForm>(() => roleFormFrom(role));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Follow the saved role when it (re)loads — unless there are local edits.
  const [synced, setSynced] = useState(role);
  if (synced !== role) {
    setSynced(role);
    if (Object.keys(roleUpdate(form, synced)).length === 0) setForm(roleFormFrom(role));
  }

  const update = roleUpdate(form, role);
  const dirty = Object.keys(update).length > 0;
  const problems = roleFormErrors(form);

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await updateWorkspace(update);
      await refresh();
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save your role.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="py-4 first:pt-0 last:pb-0">
      <div className="text-xs font-medium text-fg">Your role</div>
      <p className="text-xs text-fg-muted mt-0.5 mb-2 leading-relaxed">
        So the Executive&apos;s advice fits your job, whatever your role: your own business, a
        team you lead, or clients you advise.
      </p>
      <RoleFields
        value={form}
        onChange={(next) => {
          setSaved(false);
          setForm(next);
        }}
        disabled={loading || busy}
        idPrefix="ws-role"
      />
      {problems.map((p) => (
        <p key={p} className="mt-1.5 text-xs text-red-400">
          {p}
        </p>
      ))}
      {error && <p className="mt-1.5 text-xs text-red-400">{error}</p>}
      <div className="mt-2.5 flex items-center gap-2">
        <button
          type="button"
          disabled={!dirty || busy || problems.length > 0}
          onClick={() => void save()}
          className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-500 hover:bg-indigo-600 text-white transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? "Saving…" : "Save role"}
        </button>
        {dirty && !busy && (
          <button
            type="button"
            onClick={() => setForm(roleFormFrom(role))}
            className="px-3 py-1.5 rounded-lg text-xs text-fg-muted hover:text-fg transition-colors cursor-pointer"
          >
            Discard changes
          </button>
        )}
        {saved && !dirty && <span className="text-xs text-fg-muted">Saved.</span>}
      </div>
    </div>
  );
}

// "Book meetings without asking" — the meeting_scheduling decision class
// between "propose" (each booking waits for approval in the briefing) and
// "auto_execute". Hidden when this backend has no such setting (404).
function MeetingAutonomySwitch() {
  const [mode, setMode] = useState<DecisionClassMode | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "absent" | "error">("loading");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getDecisionClassMode(MEETING_SCHEDULING_CLASS, controller.signal)
      .then((setting) => {
        if (!setting) {
          setState("absent");
          return;
        }
        setMode(setting.mode);
        setState("ready");
      })
      .catch((err) => {
        if ((err as Error)?.name === "AbortError") return;
        setState("error");
      });
    return () => controller.abort();
  }, []);

  if (state === "absent" || state === "loading") return null;
  if (state === "error") {
    return (
      <div className="py-4 first:pt-0 last:pb-0">
        <p className="text-xs text-fg-subtle">Couldn&apos;t load the meeting-booking setting.</p>
      </div>
    );
  }

  const on = mode === "auto_execute";
  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      const next = await setDecisionClassMode(MEETING_SCHEDULING_CLASS, on ? "propose" : "auto_execute");
      setMode(next.mode);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="py-4 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium text-fg" id="ws-meetings-label">
            Book meetings without asking
          </div>
          <p className="text-xs text-fg-muted mt-0.5 leading-relaxed">
            {on
              ? "The Executive books meetings on your calendar on its own."
              : "Each meeting the Executive wants to book waits for your approval in the briefing."}
          </p>
        </div>
        <Switch checked={on} onChange={() => void toggle()} disabled={busy} labelledBy="ws-meetings-label" />
      </div>
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  );
}
