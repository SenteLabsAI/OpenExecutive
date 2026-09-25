"use client";

import { useParams, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { TeamModeOffer } from "@/components/people/TeamModeOffer";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  archivePerson,
  closeOpenLoop,
  getPeopleViewer,
  getPerson,
  getPersonOpenLoops,
  getPersonOutreach,
  getPersonWorkingStyle,
  resetPersonWorkingStyle,
  savePersonWorkingStyle,
  updatePerson,
  type AvailabilityWindow,
  type OpenLoop,
  type OutreachStat,
  type Person,
  type PersonKind,
  type WorkingStyle,
} from "@/lib/api";
import { isContact, shouldOfferTeamMode } from "@/lib/peopleKinds";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ALL_SCOPES = [
  { value: "spend_lt_2k", label: "Spend <$2K", hint: "Receives proposals for any spend under $2K." },
  { value: "spend_lt_10k", label: "Spend <$10K", hint: "Receives proposals for spend under $10K." },
  { value: "spend_gt_10k", label: "Spend >$10K", hint: "Receives proposals for spend over $10K." },
  { value: "hiring_signoff", label: "Hiring", hint: "Receives proposals related to hiring decisions." },
  { value: "vendor_onboarding", label: "Vendors", hint: "Receives proposals for vendor contracts." },
  { value: "customer_credit", label: "Credit", hint: "Receives proposals involving credit or debt." },
  { value: "legal_sign", label: "Legal", hint: "Receives proposals with legal implications." },
  { value: "board_comms", label: "Board", hint: "Receives proposals before board communications." },
  { value: "wildcard", label: "All (wildcard)", hint: "Receives anything no one else is scoped for — usually the principal." },
];

const CHANNELS = ["any", "slack", "discord", "telegram", "email"];

// ---------------------------------------------------------------------------
// How they respond to outreach (attunement outcome ledger)
// ---------------------------------------------------------------------------

function OutreachSection({ personId }: { personId: number }) {
  const [rows, setRows] = useState<OutreachStat[] | null>(null);

  useEffect(() => {
    getPersonOutreach(personId)
      .then(setRows)
      .catch(() => setRows([]));
  }, [personId]);

  if (!rows || rows.length === 0) return null;
  return (
    <section className="mb-6">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-1">
        How they respond
      </h2>
      <p className="text-xs text-fg-muted mb-3">
        Proactive messages over the last 30 days. Kinds they reliably ignore are
        sent less often.
      </p>
      <div className="space-y-1.5">
        {rows.map((r) => {
          const answered = r.replied + r.acted;
          const resolved = answered + r.ignored;
          return (
            <div
              key={r.source}
              className="flex items-center justify-between gap-3 px-4 py-2 rounded-lg border border-line bg-surface-elevated text-sm"
            >
              <span className="text-fg capitalize">{r.label}</span>
              <span className="text-xs text-fg-muted">
                {resolved > 0 ? `${answered} of ${resolved} answered` : "no answers yet"}
                {r.pending > 0 ? ` · ${r.pending} pending` : ""}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// How I work with them — learned working style (attunement)
// ---------------------------------------------------------------------------

const MAX_STYLE_RULES = 4;
const STYLE_TEXTAREA_ROWS = 4;

function WorkingStyleSection({ personId }: { personId: number }) {
  const [style, setStyle] = useState<WorkingStyle | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getPersonWorkingStyle(personId)
      .then(setStyle)
      .catch(() => setStyle(null));
  }, [personId]);

  async function run(action: () => Promise<WorkingStyle>) {
    setBusy(true);
    setError(null);
    try {
      setStyle(await action());
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setBusy(false);
    }
  }

  if (!style) return null;
  const rules = draft
    .split("\n")
    .map((line) => line.replace(/^[-•]\s*/, "").trim())
    .filter(Boolean);
  return (
    <section className="mb-6">
      <div className="flex items-center justify-between mb-1">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
          How I work with them
        </h2>
        {!editing && (
          <div className="flex gap-3 text-xs">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setDraft(style.rules.map((r) => r.text).join("\n"));
                setEditing(true);
              }}
              className="text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
            >
              Edit
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                run(() => savePersonWorkingStyle(personId, null, !style.locked))
              }
              className="text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
            >
              {style.locked ? "Unlock" : "Lock"}
            </button>
            {style.rules.length > 0 && (
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    await resetPersonWorkingStyle(personId);
                    return getPersonWorkingStyle(personId);
                  })
                }
                className="text-fg-muted hover:text-fg disabled:opacity-50"
              >
                Reset
              </button>
            )}
          </div>
        )}
      </div>
      <p className="text-xs text-fg-muted mb-3">
        How replies are written for them, learned from their own 👍/👎 and requests.
        {style.locked
          ? " Locked — kept as is."
          : " Updated as they use it; rules you type are always kept. Lock it to stop learning."}
      </p>
      {error && <p className="text-xs text-rose-300 mb-2">{error}</p>}
      {editing ? (
        <div className="space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={STYLE_TEXTAREA_ROWS}
            placeholder="One rule per line, e.g. Lead with the recommendation, then the numbers."
            className="w-full px-3 py-2 rounded-lg border border-line bg-surface-elevated text-sm text-fg"
          />
          {rules.length > MAX_STYLE_RULES && (
            <p className="text-xs text-amber-300">At most {MAX_STYLE_RULES} rules.</p>
          )}
          <div className="flex gap-3 text-xs">
            <button
              type="button"
              disabled={busy || rules.length > MAX_STYLE_RULES}
              onClick={() => run(() => savePersonWorkingStyle(personId, rules, style.locked))}
              className="text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
            >
              {busy ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(false)}
              className="text-fg-muted hover:text-fg disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : style.rules.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing learned yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {style.rules.map((r) => (
            <li
              key={r.text}
              className="px-4 py-2 rounded-lg border border-line bg-surface-elevated text-sm text-fg"
            >
              {r.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Open loops — what this person owes (attunement)
// ---------------------------------------------------------------------------

function OpenLoopsSection({ personId }: { personId: number }) {
  const [loops, setLoops] = useState<OpenLoop[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [closing, setClosing] = useState<number | null>(null);

  useEffect(() => {
    getPersonOpenLoops(personId)
      .then(setLoops)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [personId]);

  async function close(loopId: number) {
    setClosing(loopId);
    setError(null);
    try {
      await closeOpenLoop(loopId, "done");
      setLoops((prev) => (prev ?? []).filter((l) => l.loop_id !== loopId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to close");
    } finally {
      setClosing(null);
    }
  }

  const now = Date.now();
  return (
    <section className="mb-6">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-1">
        Open loops
      </h2>
      <p className="text-xs text-fg-muted mb-3">
        Things they committed to or were asked for in conversation. Overdue ones are
        followed up automatically; close one once it&apos;s done.
      </p>
      {error && <p className="text-xs text-rose-300 mb-2">{error}</p>}
      {loops === null ? (
        !error && <p className="text-sm text-fg-muted">Loading…</p>
      ) : loops.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing open.</p>
      ) : (
        <div className="space-y-2">
          {loops.map((l) => {
            const overdue = new Date(l.due_at).getTime() <= now;
            return (
              <div
                key={l.loop_id}
                className="flex items-start justify-between gap-3 px-4 py-2.5 rounded-lg border border-line bg-surface-elevated text-sm"
              >
                <div className="min-w-0">
                  <p className="text-fg">{l.description}</p>
                  <p className={`text-xs mt-0.5 ${overdue ? "text-amber-300" : "text-fg-muted"}`}>
                    {overdue ? "Overdue since " : "Due "}
                    {new Date(l.due_at).toLocaleDateString()}
                  </p>
                </div>
                <button
                  type="button"
                  disabled={closing === l.loop_id}
                  onClick={() => close(l.loop_id)}
                  className="flex-shrink-0 text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
                >
                  {closing === l.loop_id ? "Closing…" : "Mark done"}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// ---------------------------------------------------------------------------
// Disclosure section — collapsible panel used in edit mode
// ---------------------------------------------------------------------------

function DisclosureSection({
  label,
  open,
  onToggle,
  children,
}: {
  label: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="border-t border-line pt-2">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex items-center justify-between w-full py-1.5 text-xs font-medium text-fg-muted hover:text-fg transition-colors"
      >
        <span>{label}</span>
        <span aria-hidden="true" className="text-fg-subtle text-[10px]">{open ? "▲" : "▼"}</span>
      </button>
      {open && <div className="pt-2 space-y-3">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Availability window row
// ---------------------------------------------------------------------------

interface WindowRowProps {
  win: AvailabilityWindow;
  onChange: (w: AvailabilityWindow) => void;
  onRemove: () => void;
}

function WindowRow({ win, onChange, onRemove }: WindowRowProps) {
  function toggleDay(d: number) {
    const days = win.weekdays.includes(d)
      ? win.weekdays.filter((x) => x !== d)
      : [...win.weekdays, d].sort();
    onChange({ ...win, weekdays: days });
  }

  return (
    <div className="rounded-lg border border-line bg-surface-input p-3 space-y-2">
      <div className="flex flex-wrap gap-1">
        {WEEKDAY_NAMES.map((name, idx) => (
          <button
            key={idx}
            type="button"
            onClick={() => toggleDay(idx)}
            className={`px-2 py-0.5 rounded text-xs font-medium border transition-colors ${
              win.weekdays.includes(idx)
                ? "bg-indigo-600/40 border-indigo-500/50 text-indigo-200"
                : "bg-surface-overlay border-line text-fg-muted hover:border-indigo-500/40"
            }`}
          >
            {name}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-3 gap-2">
        <label className="text-[10px] text-fg-muted flex flex-col gap-0.5">
          Start
          <input
            type="time"
            value={win.start_local}
            onChange={(e) => onChange({ ...win, start_local: e.target.value })}
            className="px-2 py-1 rounded bg-surface-elevated border border-line text-xs focus:outline-none focus:border-indigo-500"
          />
        </label>
        <label className="text-[10px] text-fg-muted flex flex-col gap-0.5">
          End
          <input
            type="time"
            value={win.end_local}
            onChange={(e) => onChange({ ...win, end_local: e.target.value })}
            className="px-2 py-1 rounded bg-surface-elevated border border-line text-xs focus:outline-none focus:border-indigo-500"
          />
        </label>
        <label className="text-[10px] text-fg-muted flex flex-col gap-0.5">
          Timezone
          <input
            value={win.timezone}
            onChange={(e) => onChange({ ...win, timezone: e.target.value })}
            className="px-2 py-1 rounded bg-surface-elevated border border-line text-xs focus:outline-none focus:border-indigo-500"
            placeholder="America/Los_Angeles"
          />
        </label>
      </div>
      {win.weekdays.length === 0 && (
        <p className="text-[10px] text-amber-400">Select at least one day.</p>
      )}
      {win.end_local <= win.start_local && win.start_local !== "" && win.end_local !== "" && (
        <p className="text-[10px] text-amber-400">End time must be after start time.</p>
      )}
      <button
        type="button"
        onClick={onRemove}
        className="text-[10px] text-rose-400 hover:text-rose-300"
      >
        Remove window
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function PersonDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { mode } = useWorkspace();
  const [offerFor, setOfferFor] = useState<string | null>(null);
  // Only the principal may move someone between team and contacts (contacts
  // are theirs alone); for anyone else the API 404s a contact's page anyway.
  const [viewerIsPrincipal, setViewerIsPrincipal] = useState(false);

  useEffect(() => {
    getPeopleViewer()
      .then((v) => setViewerIsPrincipal(v.is_principal))
      .catch(() => setViewerIsPrincipal(false));
  }, []);
  const rawId = params?.id;
  const personId = rawId ? parseInt(String(rawId), 10) : NaN;

  const [person, setPerson] = useState<Person | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Edit-mode disclosure panels
  const [showContact, setShowContact] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Edit form state — mirrors the person fields we allow editing
  const [form, setForm] = useState({
    full_name: "",
    role: "",
    kind: "team" as PersonKind,
    email: "",
    slack_user_id: "",
    telegram_chat_id: "",
    discord_user_id: "",
    preferred_channel: "any",
    response_sla_hours: "24",
    on_leave_until: "",
    authority_scope: [] as string[],
    availability: [] as AvailabilityWindow[],
  });

  useEffect(() => {
    if (isNaN(personId)) return;
    getPerson(personId)
      .then((p) => {
        setPerson(p);
        resetForm(p);
        // Auto-open sections that already have data so existing values aren't hidden
        if (p.email || p.slack_user_id || p.discord_user_id || p.telegram_chat_id || p.preferred_channel !== "any" || p.response_sla_hours !== 24) {
          setShowContact(true);
        }
        if (p.on_leave_until) {
          setShowAdvanced(true);
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [personId]);

  // Contacts are outside the team: no approvals, no reply SLA, no
  // availability windows, no departments — and nothing to chase.
  const contact = person ? isContact(person) : false;
  const formContact = form.kind === "contact" && !person?.is_principal;

  function resetForm(p: Person) {
    setForm({
      full_name: p.full_name,
      role: p.role,
      kind: p.kind ?? "team",
      email: p.email ?? "",
      slack_user_id: p.slack_user_id ?? "",
      telegram_chat_id: p.telegram_chat_id ?? "",
      discord_user_id: p.discord_user_id ?? "",
      preferred_channel: p.preferred_channel,
      response_sla_hours: String(p.response_sla_hours),
      on_leave_until: p.on_leave_until ?? "",
      authority_scope: [...p.authority_scope],
      availability: p.availability.map((w) => ({ ...w, weekdays: [...w.weekdays] })),
    });
  }

  function toggleScope(val: string) {
    setForm((f) => ({
      ...f,
      authority_scope: f.authority_scope.includes(val)
        ? f.authority_scope.filter((s) => s !== val)
        : [...f.authority_scope, val],
    }));
  }

  function addWindow() {
    setForm((f) => ({
      ...f,
      availability: [
        ...f.availability,
        { weekdays: [1], start_local: "09:00", end_local: "17:00", timezone: "UTC" },
      ],
    }));
  }

  function updateWindow(i: number, w: AvailabilityWindow) {
    setForm((f) => {
      const updated = [...f.availability];
      updated[i] = w;
      return { ...f, availability: updated };
    });
  }

  function removeWindow(i: number) {
    setForm((f) => ({
      ...f,
      availability: f.availability.filter((_, idx) => idx !== i),
    }));
  }

  async function save() {
    const trimmedName = form.full_name.trim();
    if (!trimmedName) {
      setSaveErr("Full name is required.");
      return;
    }
    setSaving(true);
    setSaveErr(null);
    try {
      const slaNum = Number(form.response_sla_hours);
      const wasContact = person ? isContact(person) : false;
      const updated = await updatePerson(personId, {
        full_name: trimmedName,
        role: form.role.trim(),
        // The principal is always on the team, and only the principal may
        // change anyone's kind; the server refuses otherwise.
        ...(person?.is_principal || !viewerIsPrincipal ? {} : { kind: form.kind }),
        email: form.email.trim() || null,
        slack_user_id: form.slack_user_id.trim() || null,
        telegram_chat_id: form.telegram_chat_id.trim() || null,
        discord_user_id: form.discord_user_id.trim() || null,
        preferred_channel: form.preferred_channel,
        response_sla_hours: slaNum >= 1 ? slaNum : 24,
        on_leave_until: form.on_leave_until || null,
        // The backend only clears the date on an explicit flag; null alone is ignored.
        clear_on_leave: !form.on_leave_until,
        // A contact approves nothing: drop scopes rather than leave them to
        // come back if the contact is later moved onto the team.
        authority_scope: formContact ? [] : form.authority_scope,
        availability: form.availability,
      });
      setPerson(updated);
      setEditing(false);
      setSaved(true);
      if (wasContact && !isContact(updated) && shouldOfferTeamMode(mode, updated.kind, updated.is_principal)) {
        setOfferFor(updated.full_name);
      }
      setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function doArchive() {
    if (!window.confirm("Archive this person? They won't receive new assignments but their history is preserved.")) return;
    setArchiving(true);
    setSaveErr(null);
    try {
      await archivePerson(personId);
      router.push("/people");
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "Archive failed");
      setArchiving(false);
    }
  }

  return (
    <div className="flex flex-col h-full bg-surface">
      {offerFor && <TeamModeOffer name={offerFor} onDone={() => setOfferFor(null)} />}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-6 py-6">
          {loading && <p className="text-fg-muted text-sm">Loading…</p>}
          {error && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
              {error}
            </div>
          )}

          {person && (
            <>
              {/* Header */}
              <div className="flex items-start gap-3 mb-6">
                <div className="w-10 h-10 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center flex-shrink-0">
                  <span className="text-white text-sm font-bold">
                    {person.full_name.charAt(0).toUpperCase()}
                  </span>
                </div>
                <div className="flex-1 min-w-0">
                  <h1 className="text-xl font-semibold text-fg">
                    {person.full_name}
                    {person.is_principal && (
                      <span className="ml-2 inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-violet-500/20 text-violet-300 border-violet-500/30 align-middle">
                        Principal
                      </span>
                    )}
                    {contact && (
                      <span className="ml-2 inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-sky-500/20 text-sky-300 border-sky-500/30 align-middle">
                        Contact
                      </span>
                    )}
                    {person.archived && (
                      <span className="ml-2 inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-rose-500/20 text-rose-300 border-rose-500/30 align-middle">
                        Archived
                      </span>
                    )}
                  </h1>
                  <p className="text-sm text-fg-muted">{person.role || "No role set"}</p>
                </div>
                <div className="flex gap-2 flex-shrink-0">
                  {saved && (
                    <span className="text-xs text-emerald-400 self-center">Saved ✓</span>
                  )}
                  {editing ? (
                    <>
                      <button
                        disabled={saving || !form.full_name.trim()}
                        onClick={save}
                        className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50"
                      >
                        {saving ? "Saving…" : "Save"}
                      </button>
                      <button
                        disabled={saving}
                        onClick={() => {
                          resetForm(person);
                          setEditing(false);
                          setSaveErr(null);
                        }}
                        className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => setEditing(true)}
                      className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay"
                    >
                      Edit
                    </button>
                  )}
                </div>
              </div>

              {saveErr && (
                <div className="mb-4 p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
                  {saveErr}
                </div>
              )}

              {/* Core fields */}
              <section className="rounded-xl border border-line bg-surface-elevated px-4 mb-6">
                {editing ? (
                  <div className="py-4 space-y-3">
                    {/* Always-visible in edit mode */}
                    <div className="grid grid-cols-2 gap-3">
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Full name
                        <input
                          value={form.full_name}
                          onChange={(e) => setForm((f) => ({ ...f, full_name: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        {formContact ? "Role and company" : "Role"}
                        <input
                          value={form.role}
                          onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                    </div>

                    {!person.is_principal && viewerIsPrincipal && (
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Team member or contact
                        <select
                          value={form.kind}
                          onChange={(e) => setForm((f) => ({ ...f, kind: e.target.value as PersonKind }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        >
                          <option value="team">Team member — can sign in, message the Executive and approve</option>
                          <option value="contact">Contact — outside the team and private to you, emailed only when you ask</option>
                        </select>
                      </label>
                    )}

                    {/* Contact & routing */}
                    <DisclosureSection
                      label="Contact & routing"
                      open={showContact}
                      onToggle={() => setShowContact((v) => !v)}
                    >
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="text-xs text-fg-muted flex flex-col gap-1">
                            Preferred channel
                            <select
                              value={form.preferred_channel}
                              onChange={(e) => setForm((f) => ({ ...f, preferred_channel: e.target.value }))}
                              className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                            >
                              {CHANNELS.map((c) => (
                                <option key={c} value={c}>{c}</option>
                              ))}
                            </select>
                          </label>
                          <p className="text-[10px] text-fg-muted mt-1">
                            Proposals routed to this person are sent via {form.preferred_channel === "any" ? "any available channel" : form.preferred_channel}.
                          </p>
                        </div>
                        {!formContact && (
                        <div>
                          <label className="text-xs text-fg-muted flex flex-col gap-1">
                            Expected reply within
                            <div className="flex items-center gap-1.5">
                              <input
                                type="number"
                                min={1}
                                value={form.response_sla_hours}
                                onChange={(e) => setForm((f) => ({ ...f, response_sla_hours: e.target.value }))}
                                className="flex-1 px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                              />
                              <span className="text-xs text-fg-muted flex-shrink-0">hours</span>
                            </div>
                          </label>
                          <p className="text-[10px] text-fg-muted mt-1">
                            Items overdue in Today after {form.response_sla_hours || 24}h with no reply.
                          </p>
                        </div>
                        )}
                      </div>

                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Email
                        <input
                          type="email"
                          value={form.email}
                          onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        />
                      </label>

                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Slack user ID
                        <input
                          value={form.slack_user_id}
                          onChange={(e) => setForm((f) => ({ ...f, slack_user_id: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                          placeholder="U01ABC123"
                        />
                      </label>

                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Discord user ID
                        <input
                          value={form.discord_user_id}
                          onChange={(e) => setForm((f) => ({ ...f, discord_user_id: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                          placeholder="123456789012345678"
                        />
                        <span className="text-[10px] text-fg-muted">
                          Right-click your Discord username and &quot;Copy User ID&quot; (developer mode required).
                        </span>
                      </label>

                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        Telegram chat ID
                        <input
                          value={form.telegram_chat_id}
                          onChange={(e) => setForm((f) => ({ ...f, telegram_chat_id: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                          placeholder="123456789"
                        />
                      </label>
                    </DisclosureSection>

                    {/* Advanced */}
                    <DisclosureSection
                      label="Advanced"
                      open={showAdvanced}
                      onToggle={() => setShowAdvanced((v) => !v)}
                    >
                      <label className="text-xs text-fg-muted flex flex-col gap-1">
                        On leave until
                        <input
                          type="date"
                          value={form.on_leave_until}
                          onChange={(e) => setForm((f) => ({ ...f, on_leave_until: e.target.value }))}
                          className="px-2 py-1.5 rounded-lg bg-surface-input border border-line text-sm focus:outline-none focus:border-indigo-500"
                        />
                      </label>
                    </DisclosureSection>
                  </div>
                ) : (
                  <div className="divide-y divide-line">
                    {[
                      ["Kind", contact ? "Contact — outside the team" : "Team member"],
                      ["Preferred channel", person.preferred_channel],
                      ...(contact ? [] : [["Expected reply within", `${person.response_sla_hours} hours`]]),
                      ["Email", person.email ?? "—"],
                      ["Slack user ID", person.slack_user_id ?? "—"],
                      ["Discord user ID", person.discord_user_id ?? "—"],
                      ["Telegram chat ID", person.telegram_chat_id ?? "—"],
                      ["On leave until", person.on_leave_until ?? "—"],
                    ].map(([label, value]) => (
                      <div key={label} className="flex items-start gap-3 py-2">
                        <div className="w-40 flex-shrink-0 text-xs text-fg-muted pt-0.5">{label}</div>
                        <div className="text-sm text-fg">{value}</div>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {/* Authority scope — a contact approves nothing */}
              {!(editing ? formContact : contact) && (
              <section className="mb-6">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-3">
                  What this person approves
                </h2>
                {editing ? (
                  <div className="grid grid-cols-3 gap-2">
                    {ALL_SCOPES.map(({ value, label, hint }) => {
                      const active = form.authority_scope.includes(value);
                      return (
                        <button
                          key={value}
                          type="button"
                          onClick={() => toggleScope(value)}
                          title={hint}
                          className={`px-2 py-2 rounded-lg border text-xs transition-colors text-left ${
                            active
                              ? "bg-indigo-600/30 border-indigo-500/50 text-indigo-300"
                              : "bg-surface-elevated border-line text-fg-muted hover:border-indigo-500/40"
                          }`}
                        >
                          <div className="font-medium">{label}</div>
                          <div className="text-[9px] leading-tight mt-0.5 opacity-70 line-clamp-2">{hint}</div>
                        </button>
                      );
                    })}
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {person.authority_scope.length === 0 ? (
                      <p className="text-sm text-fg-muted">No approval authority.</p>
                    ) : (
                      person.authority_scope.map((s) => {
                        const entry = ALL_SCOPES.find((x) => x.value === s);
                        return (
                          <span
                            key={s}
                            className={`inline-block px-2 py-1 rounded-lg border text-xs font-medium ${
                              s === "wildcard"
                                ? "bg-indigo-500/20 text-indigo-300 border-indigo-500/30"
                                : "bg-surface-input/40 text-fg border-line"
                            }`}
                          >
                            {entry?.label ?? s}
                          </span>
                        );
                      })
                    )}
                  </div>
                )}
              </section>

              )}

              {/* Availability windows — contacts are never chased */}
              {!(editing ? formContact : contact) && (
              <section className="mb-6">
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">
                    Availability Windows
                  </h2>
                  {editing && (
                    <button
                      type="button"
                      onClick={addWindow}
                      className="text-xs text-indigo-400 hover:text-indigo-300"
                    >
                      + Add window
                    </button>
                  )}
                </div>

                {editing ? (
                  <div className="space-y-2">
                    {form.availability.length === 0 && (
                      <p className="text-sm text-fg-muted">
                        No windows — person is considered always available.{" "}
                        <button onClick={addWindow} className="text-indigo-400 hover:underline">
                          Add one →
                        </button>
                      </p>
                    )}
                    {form.availability.map((w, i) => (
                      <WindowRow
                        key={i}
                        win={w}
                        onChange={(updated) => updateWindow(i, updated)}
                        onRemove={() => removeWindow(i)}
                      />
                    ))}
                  </div>
                ) : (
                  <div>
                    {person.availability.length === 0 ? (
                      <p className="text-sm text-fg-muted">Always available (no windows set).</p>
                    ) : (
                      <div className="space-y-2">
                        {person.availability.map((w, i) => (
                          <div
                            key={i}
                            className="flex items-center gap-3 px-4 py-2.5 rounded-lg border border-line bg-surface-elevated text-sm"
                          >
                            <div className="flex gap-1">
                              {w.weekdays.map((d) => (
                                <span key={d} className="px-1.5 py-0.5 rounded bg-surface-overlay text-xs text-fg">
                                  {WEEKDAY_NAMES[d] ?? d}
                                </span>
                              ))}
                            </div>
                            <span className="text-fg">
                              {w.start_local} – {w.end_local}
                            </span>
                            <span className="text-fg-muted text-xs">{w.timezone}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </section>

              )}

              {!person.archived && !contact && <OpenLoopsSection personId={personId} />}
              {!person.archived && !contact && <OutreachSection personId={personId} />}
              {!person.archived && !contact && <WorkingStyleSection personId={personId} />}

              {/* Archive */}
              {!person.is_principal && !person.archived && (
                <section className="border-t border-line pt-6">
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-fg-muted mb-2">
                    Danger zone
                  </h2>
                  <p className="text-xs text-fg-muted mb-3">
                    Archiving removes this person from routing and the active roster. Their history is preserved.
                  </p>
                  <button
                    disabled={archiving}
                    onClick={doArchive}
                    className="px-4 py-2 text-sm rounded-lg bg-rose-500/10 hover:bg-rose-500/20 border border-rose-500/30 text-rose-300 disabled:opacity-50"
                  >
                    {archiving ? "Archiving…" : "Archive person"}
                  </button>
                </section>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
