"use client";

import { useEffect, useMemo, useState } from "react";

import AdvancedFold from "@/components/settings/AdvancedFold";
import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import Button from "@/components/ui/Button";
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
// whether it books meetings without asking, as one card each, with company
// email domains under Advanced. Mode, role and zone go through PUT
// /workspace and then the app-wide WorkspaceProvider is refreshed so the nav
// and pages follow. The page supplies the title; this is the body.

export const MODE_LABEL: Record<WorkspaceMode, string> = {
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

  const browserZone = useMemo(() => browserTimeZone(), []);
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

  const select =
    "w-full h-11 px-3 rounded-xl text-[15px] bg-surface border border-line text-fg focus:outline-none focus:border-line-strong disabled:opacity-60";
  return (
    <>
      {error && (
        <p className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-500">
          {error}
        </p>
      )}

      <SettingsCard
        title="Using Open Executive"
        titleId="ws-mode-label"
        description="Just for you, or for you and your team."
      >
        <div
          role="radiogroup"
          aria-labelledby="ws-mode-label"
          className="inline-flex w-full sm:w-auto rounded-xl border border-line p-1 bg-surface"
        >
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
                className={`flex-1 sm:flex-none h-10 px-5 rounded-lg text-[15px] font-medium transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                  selected ? "bg-surface-elevated text-fg shadow-sm" : "text-fg-muted hover:text-fg"
                }`}
              >
                {MODE_LABEL[m]}
              </button>
            );
          })}
        </div>

        {pendingMode && pendingMode !== mode && (
          <div className="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
            <p className="text-sm text-fg leading-relaxed">
              Switch to <span className="font-semibold">{MODE_LABEL[pendingMode]}</span>?{" "}
              {SWITCH_EFFECT[pendingMode]}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button
                variant="primary"
                disabled={busy}
                onClick={async () => {
                  if (await save({ mode: pendingMode })) setPendingMode(null);
                }}
              >
                {busy ? "Switching…" : `Switch to ${MODE_LABEL[pendingMode]}`}
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => setPendingMode(null)}>
                Cancel
              </Button>
            </div>
          </div>
        )}
        <p className="mt-4 text-[13px] text-fg-subtle">
          A persona you customised in Council stays in place in either mode.
        </p>
      </SettingsCard>

      {mode === "solo" && <RoleSection />}

      <SettingsCard
        title={<label htmlFor="ws-timezone">Time zone</label>}
        description={
          <>
            When your morning brief, end-of-day digest and reflection arrive, and how the Executive
            reads &ldquo;tomorrow at 9&rdquo;.
            {effectiveTimezone && <> Now: {effectiveTimezone}.</>}
          </>
        }
      >
        <select
          id="ws-timezone"
          value={timezone ?? ""}
          disabled={loading || busy}
          onChange={(e) => void save({ timezone: e.target.value || null })}
          className={select}
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
            className="mt-2 min-h-touch text-left text-[15px] font-medium text-accent hover:underline cursor-pointer disabled:opacity-50"
          >
            Use this browser&apos;s time zone ({browserZone})
          </button>
        )}
      </SettingsCard>

      <MeetingAutonomySwitch />

      <CompanyDomainsSection />
    </>
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
    <AdvancedFold id="ws-advanced" summary="Company email domains">
      <SettingsCard
        title={<label htmlFor="ws-domains">Company email domains</label>}
        description={
          <>
            Mail from these domains matches a teammate by the part before the @, so
            anna+invoices@ reaches the Anna already on your People list. Someone new writing from
            one is suggested as a teammate — you still confirm them.
            {!custom && " Taken from your own address until you set them."}
          </>
        }
      >
        <input
          id="ws-domains"
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="acme.com, acme.io"
          className="w-full h-11 px-3.5 rounded-xl text-[15px] bg-surface border border-line text-fg focus:outline-none focus:border-line-strong disabled:opacity-60"
        />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            disabled={busy || parsed.join(",") === domains.join(",")}
            onClick={() => void save(parsed.length ? parsed : null)}
          >
            Save
          </Button>
          {custom && (
            <Button variant="ghost" disabled={busy} onClick={() => void save(null)}>
              Use my address
            </Button>
          )}
        </div>
        {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
      </SettingsCard>
    </AdvancedFold>
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
    <SettingsCard
      title="Your role"
      description="So the Executive's advice fits your job, whatever your role: your own business, a team you lead, or clients you advise."
    >
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
        <p key={p} className="mt-2 text-sm text-red-500">
          {p}
        </p>
      ))}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          disabled={!dirty || busy || problems.length > 0}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : "Save role"}
        </Button>
        {dirty && !busy && (
          <Button variant="ghost" onClick={() => setForm(roleFormFrom(role))}>
            Discard changes
          </Button>
        )}
        {saved && !dirty && <span className="text-sm text-fg-muted">Saved.</span>}
      </div>
    </SettingsCard>
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
      <SettingsCard>
        <p className="text-sm text-fg-muted">Couldn&apos;t load the meeting-booking setting.</p>
      </SettingsCard>
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
    <SettingsCard
      title="Book meetings without asking"
      titleId="ws-meetings-label"
      description={
        on
          ? "The Executive books meetings on your calendar on its own."
          : "Each meeting the Executive wants to book waits for your approval in the briefing."
      }
      action={<Switch checked={on} onChange={() => void toggle()} disabled={busy} labelledBy="ws-meetings-label" />}
    >
      {error && <p className="text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
