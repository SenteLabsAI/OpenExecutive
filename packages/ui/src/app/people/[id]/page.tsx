"use client";

import { useParams, useRouter } from "next/navigation";
import { type FormEvent, type ReactNode, useEffect, useId, useState } from "react";

import { TeamModeOffer } from "@/components/people/TeamModeOffer";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  archivePerson,
  assignOpenLoop,
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
  { value: "meeting_scheduling", label: "Meetings", hint: "Receives meetings the Executive wants to book." },
  { value: "wildcard", label: "All (wildcard)", hint: "Receives anything no one else is scoped for — usually the principal." },
];

const CHANNELS = ["any", "slack", "discord", "telegram", "email"];

const INPUT_CLS =
  "w-full h-11 px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";
const LABEL_CLS = "text-sm text-fg-muted flex flex-col gap-1.5";
const HINT_CLS = "text-[13px] text-fg-subtle mt-1";
const SECTION_TITLE_CLS = "text-lg font-semibold text-fg";
const INTRO_CLS = "text-[15px] text-fg-muted mt-1 mb-4";
const ROW_CLS = "px-4 py-3 rounded-xl border border-line bg-surface-elevated text-[15px]";

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

  if (!rows) return <p className="text-[15px] text-fg-muted">Loading…</p>;
  return (
    <section>
      <h2 className={SECTION_TITLE_CLS}>How they respond</h2>
      <p className={INTRO_CLS}>
        Proactive messages over the last 30 days. Kinds they reliably ignore are
        sent less often.
      </p>
      {rows.length === 0 && (
        <p className="text-[15px] text-fg-muted">No proactive messages to them in the last 30 days.</p>
      )}
      <div className="space-y-2">
        {rows.map((r) => {
          const answered = r.replied + r.acted;
          const resolved = answered + r.ignored;
          return (
            <div
              key={r.source}
              className={`flex flex-wrap items-center justify-between gap-x-3 gap-y-1 ${ROW_CLS}`}
            >
              <span className="text-fg capitalize">{r.label}</span>
              <span className="text-sm text-fg-muted">
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

  if (!style) return <p className="text-[15px] text-fg-muted">Loading…</p>;
  const rules = draft
    .split("\n")
    .map((line) => line.replace(/^[-•]\s*/, "").trim())
    .filter(Boolean);
  return (
    <section>
      <div className="flex items-start justify-between gap-3">
        <h2 className={SECTION_TITLE_CLS}>How I work with them</h2>
        {!editing && (
          <div className="flex items-center gap-1 flex-shrink-0 -mt-1.5">
            <Button
              size="md"
              disabled={busy}
              onClick={() => {
                setDraft(style.rules.map((r) => r.text).join("\n"));
                setEditing(true);
              }}
            >
              Edit rules
            </Button>
            <OverflowMenu
              label="More working-style actions"
              items={[
                {
                  label: style.locked ? "Unlock (keep learning)" : "Lock (stop learning)",
                  disabled: busy,
                  onSelect: () => void run(() => savePersonWorkingStyle(personId, null, !style.locked)),
                },
                ...(style.rules.length > 0
                  ? [{
                      label: "Reset what was learned",
                      danger: true,
                      disabled: busy,
                      onSelect: () =>
                        void run(async () => {
                          await resetPersonWorkingStyle(personId);
                          return getPersonWorkingStyle(personId);
                        }),
                    }]
                  : []),
              ]}
            />
          </div>
        )}
      </div>
      <p className={INTRO_CLS}>
        How replies are written for them, learned from their own 👍/👎 and requests.
        {style.locked
          ? " Locked — kept as is."
          : " Updated as they use it; rules you type are always kept. Lock it to stop learning."}
      </p>
      {error && <p className="text-sm text-rose-500 mb-3">{error}</p>}
      {editing ? (
        <div className="space-y-3">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={STYLE_TEXTAREA_ROWS}
            placeholder="One rule per line, e.g. Lead with the recommendation, then the numbers."
            className="w-full px-3 py-2.5 rounded-xl border border-line bg-surface-input/60 text-[15px] text-fg focus:outline-none focus:border-accent"
          />
          {rules.length > MAX_STYLE_RULES && (
            <p className="text-sm text-amber-500">At most {MAX_STYLE_RULES} rules.</p>
          )}
          <div className="flex gap-2">
            <Button
              variant="primary"
              disabled={busy || rules.length > MAX_STYLE_RULES}
              onClick={() => run(() => savePersonWorkingStyle(personId, rules, style.locked))}
            >
              {busy ? "Saving…" : "Save"}
            </Button>
            <Button disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : style.rules.length === 0 ? (
        <p className="text-[15px] text-fg-muted">Nothing learned yet.</p>
      ) : (
        <ul className="space-y-2">
          {style.rules.map((r) => (
            <li
              key={r.text}
              className={`${ROW_CLS} text-fg`}
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

function OpenLoopsSection({
  personId,
  canList,
  canAssign,
}: {
  personId: number;
  // The principal or this person: what someone owes is not roster-public.
  canList: boolean;
  // Anyone on the team may assign them a task (the API decides).
  canAssign: boolean;
}) {
  const [loops, setLoops] = useState<OpenLoop[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [closing, setClosing] = useState<number | null>(null);
  // Sampled once at mount: a render must not call Date.now() (react-hooks/purity).
  const [mountedAt] = useState(() => Date.now());
  const [task, setTask] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [assigning, setAssigning] = useState(false);
  const [assignError, setAssignError] = useState<string | null>(null);
  const [assigned, setAssigned] = useState<string | null>(null);

  useEffect(() => {
    if (!canList) return;
    getPersonOpenLoops(personId)
      .then(setLoops)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [personId, canList]);

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

  async function assign(e: FormEvent) {
    e.preventDefault();
    const text = task.trim();
    if (!text || assigning) return;
    setAssigning(true);
    setAssignError(null);
    setAssigned(null);
    try {
      const loop = await assignOpenLoop(personId, text, dueDate || undefined);
      if (canList) {
        setLoops((prev) =>
          [...(prev ?? []), loop].sort((a, b) => a.due_at.localeCompare(b.due_at)),
        );
      }
      setAssigned(
        `Assigned — due ${new Date(loop.due_at).toLocaleDateString()}. They'll be followed up if it isn't done by then.`,
      );
      setTask("");
      setDueDate("");
    } catch (err) {
      setAssignError(err instanceof Error ? err.message : "Couldn't assign the task");
    } finally {
      setAssigning(false);
    }
  }

  if (!canList && !canAssign) return null;
  const now = mountedAt;
  return (
    <section className="mt-8">
      <h2 className={SECTION_TITLE_CLS}>Open loops</h2>
      <p className={INTRO_CLS}>
        Things they committed to, were asked for in conversation, or were assigned here.
        Overdue ones are followed up automatically; close one once it&apos;s done.
      </p>
      {canList && (
        <>
          {error && <p className="text-sm text-rose-500 mb-3">{error}</p>}
          {loops === null ? (
            !error && <p className="text-[15px] text-fg-muted">Loading…</p>
          ) : loops.length === 0 ? (
            <p className="text-[15px] text-fg-muted">Nothing open.</p>
          ) : (
            <div className="space-y-2">
              {loops.map((l) => {
                const overdue = new Date(l.due_at).getTime() <= now;
                return (
                  <div
                    key={l.loop_id}
                    className={`flex items-center justify-between gap-3 ${ROW_CLS}`}
                  >
                    <div className="min-w-0">
                      <p className="text-fg">{l.description}</p>
                      <p className={`text-sm mt-0.5 ${overdue ? "text-amber-600 dark:text-amber-400" : "text-fg-muted"}`}>
                        {overdue ? "Overdue since " : "Due "}
                        {new Date(l.due_at).toLocaleDateString()}
                      </p>
                    </div>
                    <Button
                      disabled={closing === l.loop_id}
                      onClick={() => close(l.loop_id)}
                      className="flex-shrink-0"
                    >
                      {closing === l.loop_id ? "Closing…" : "Mark done"}
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
      {canAssign && (
        <form onSubmit={assign} className="mt-4 flex flex-col sm:flex-row gap-3 sm:items-end">
          <label className={`flex-1 ${LABEL_CLS}`}>
            Assign a task
            <input
              value={task}
              onChange={(e) => setTask(e.target.value)}
              maxLength={200}
              placeholder="e.g. send the vendor quote"
              className={INPUT_CLS}
            />
          </label>
          <label className={LABEL_CLS}>
            Due (optional)
            <input
              type="date"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
              className={INPUT_CLS}
            />
          </label>
          <Button type="submit" variant="primary" disabled={assigning || !task.trim()}>
            {assigning ? "Assigning…" : "Assign"}
          </Button>
        </form>
      )}
      {assignError && <p className="text-sm text-rose-500 mt-2">{assignError}</p>}
      {assigned && <p className="text-sm text-fg-muted mt-2">{assigned}</p>}
    </section>
  );
}

type PersonTab = "overview" | "approvals" | "respond" | "style";

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
        className="flex items-center justify-between w-full min-h-11 py-2 text-[15px] font-medium text-fg hover:text-accent transition-colors"
      >
        <span>{label}</span>
        <span aria-hidden="true" className="text-fg-subtle text-xs">{open ? "▲" : "▼"}</span>
      </button>
      {open && <div className="pt-2 pb-1 space-y-4">{children}</div>}
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
    <div className="rounded-xl border border-line bg-surface-elevated p-4 space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {WEEKDAY_NAMES.map((name, idx) => (
          <button
            key={idx}
            type="button"
            onClick={() => toggleDay(idx)}
            aria-pressed={win.weekdays.includes(idx)}
            className={`h-10 min-w-[3rem] px-3 rounded-xl text-sm font-medium border transition-colors ${
              win.weekdays.includes(idx)
                ? "bg-accent/10 border-accent/60 text-fg"
                : "bg-surface-elevated border-line text-fg-muted hover:border-line-strong"
            }`}
          >
            {name}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <label className={LABEL_CLS}>
          Start
          <input
            type="time"
            value={win.start_local}
            onChange={(e) => onChange({ ...win, start_local: e.target.value })}
            className={INPUT_CLS}
          />
        </label>
        <label className={LABEL_CLS}>
          End
          <input
            type="time"
            value={win.end_local}
            onChange={(e) => onChange({ ...win, end_local: e.target.value })}
            className={INPUT_CLS}
          />
        </label>
        <label className={LABEL_CLS}>
          Timezone
          <input
            value={win.timezone}
            onChange={(e) => onChange({ ...win, timezone: e.target.value })}
            className={INPUT_CLS}
            placeholder="America/Los_Angeles"
          />
        </label>
      </div>
      {win.weekdays.length === 0 && (
        <p className="text-sm text-amber-600 dark:text-amber-400">Select at least one day.</p>
      )}
      {win.end_local <= win.start_local && win.start_local !== "" && win.end_local !== "" && (
        <p className="text-sm text-amber-600 dark:text-amber-400">End time must be after start time.</p>
      )}
      <Button variant="danger" onClick={onRemove}>
        Remove window
      </Button>
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
  // The viewer's own People entry, if any: who may see and assign open loops.
  const [viewerPersonId, setViewerPersonId] = useState<number | null>(null);

  useEffect(() => {
    getPeopleViewer()
      .then((v) => {
        setViewerIsPrincipal(v.is_principal);
        setViewerPersonId(v.person_id);
      })
      .catch(() => {
        setViewerIsPrincipal(false);
        setViewerPersonId(null);
      });
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

  const [tab, setTab] = useState<PersonTab>("overview");
  const tabsId = useId();

  // Edit-mode disclosure panels
  const [showContact, setShowContact] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Edit form state — mirrors the person fields we allow editing
  const [form, setForm] = useState({
    full_name: "",
    role: "",
    kind: "team" as PersonKind,
    email: "",
    // Other addresses, one per line or comma-separated.
    email_aliases: "",
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
        if (p.email || (p.email_aliases?.length ?? 0) > 0 || p.slack_user_id || p.discord_user_id || p.telegram_chat_id || p.preferred_channel !== "any" || p.response_sla_hours !== 24) {
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
      email_aliases: (p.email_aliases ?? []).join("\n"),
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
        email_aliases: form.email_aliases.split(/[\s,;]+/).map((a) => a.trim()).filter(Boolean),
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
    if (!window.confirm("Archive this person? Archiving removes them from routing and the active roster: they won't receive new assignments, but their history is preserved.")) return;
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

  // Contacts get the Overview alone: they approve nothing, have no
  // availability and are never chased. An archived person keeps their
  // approvals and availability on record but nothing new is learned.
  const contactView = editing ? formContact : contact;
  const tabs: { id: PersonTab; label: string }[] = person
    ? [
        { id: "overview", label: "Overview" },
        ...(contactView ? [] : [{ id: "approvals" as const, label: "Approvals & availability" }]),
        ...(person.archived || contact
          ? []
          : [
              { id: "respond" as const, label: "How they respond" },
              { id: "style" as const, label: "How I work with them" },
            ]),
      ]
    : [];
  const activeTab: PersonTab = tabs.some((t) => t.id === tab) ? tab : "overview";
  const menuItems =
    person && !person.is_principal && !person.archived
      ? [{ label: archiving ? "Archiving…" : "Archive person", danger: true, disabled: archiving, onSelect: () => void doArchive() }]
      : [];

  return (
    <div className="flex flex-col h-full bg-surface">
      {offerFor && <TeamModeOffer name={offerFor} onDone={() => setOfferFor(null)} />}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px]">
              {error}
            </div>
          )}

          {person && (
            <>
              {/* Header */}
              <div className="flex flex-wrap items-start gap-4 mb-6">
                <div className="w-14 h-14 rounded-full bg-accent/10 text-accent flex items-center justify-center flex-shrink-0 text-xl font-bold">
                  {person.full_name.charAt(0).toUpperCase()}
                </div>
                <div className="flex-1 min-w-[12rem]">
                  <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">
                    {person.full_name}
                  </h1>
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <span className="text-[15px] text-fg-muted">{person.role || "No role set"}</span>
                    {person.is_principal && (
                      <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-accent/10 text-accent">
                        Principal
                      </span>
                    )}
                    {contact && (
                      <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-sky-500/10 text-sky-600 dark:text-sky-400">
                        Contact
                      </span>
                    )}
                    {person.archived && (
                      <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-rose-500/10 text-rose-500">
                        Archived
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  {saved && (
                    <span className="text-sm text-emerald-600 dark:text-emerald-400">Saved ✓</span>
                  )}
                  {editing ? (
                    <>
                      <Button
                        variant="primary"
                        disabled={saving || !form.full_name.trim()}
                        onClick={save}
                      >
                        {saving ? "Saving…" : "Save"}
                      </Button>
                      <Button
                        disabled={saving}
                        onClick={() => {
                          resetForm(person);
                          setEditing(false);
                          setSaveErr(null);
                        }}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button variant="primary" onClick={() => setEditing(true)}>
                      Edit
                    </Button>
                  )}
                  <OverflowMenu label={`More actions for ${person.full_name}`} items={menuItems} />
                </div>
              </div>

              {saveErr && (
                <div className="mb-4 p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px]">
                  {saveErr}
                </div>
              )}

              {tabs.length > 1 && (
                <div className="mb-6">
                  <SectionTabs idBase={tabsId} label="About this person" tabs={tabs} active={activeTab} onChange={setTab} />
                </div>
              )}

              <div {...(tabs.length > 1 ? sectionPanelProps(tabsId, activeTab) : {})}>
              {activeTab === "overview" && (
                <>
              {/* Core fields */}
              <section className="rounded-2xl border border-line bg-surface-elevated px-5">
                {editing ? (
                  <div className="py-5 space-y-4">
                    {/* Always-visible in edit mode */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <label className={LABEL_CLS}>
                        Full name
                        <input
                          value={form.full_name}
                          onChange={(e) => setForm((f) => ({ ...f, full_name: e.target.value }))}
                          className={INPUT_CLS}
                        />
                      </label>
                      <label className={LABEL_CLS}>
                        {formContact ? "Role and company" : "Role"}
                        <input
                          value={form.role}
                          onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
                          className={INPUT_CLS}
                        />
                      </label>
                    </div>

                    {!person.is_principal && viewerIsPrincipal && (
                      <label className={LABEL_CLS}>
                        Team member or contact
                        <select
                          value={form.kind}
                          onChange={(e) => setForm((f) => ({ ...f, kind: e.target.value as PersonKind }))}
                          className={INPUT_CLS}
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
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <div>
                          <label className={LABEL_CLS}>
                            Preferred channel
                            <select
                              value={form.preferred_channel}
                              onChange={(e) => setForm((f) => ({ ...f, preferred_channel: e.target.value }))}
                              className={INPUT_CLS}
                            >
                              {CHANNELS.map((c) => (
                                <option key={c} value={c}>{c}</option>
                              ))}
                            </select>
                          </label>
                          <p className={HINT_CLS}>
                            Proposals routed to this person are sent via {form.preferred_channel === "any" ? "any available channel" : form.preferred_channel}.
                          </p>
                        </div>
                        {!formContact && (
                        <div>
                          <label className={LABEL_CLS}>
                            Expected reply within
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={1}
                                value={form.response_sla_hours}
                                onChange={(e) => setForm((f) => ({ ...f, response_sla_hours: e.target.value }))}
                                className={`${INPUT_CLS} flex-1 min-w-0`}
                              />
                              <span className="text-sm text-fg-muted flex-shrink-0">hours</span>
                            </div>
                          </label>
                          <p className={HINT_CLS}>
                            Items show as overdue on Home after {form.response_sla_hours || 24}h with no reply.
                          </p>
                        </div>
                        )}
                      </div>

                      <label className={LABEL_CLS}>
                        Email
                        <input
                          type="email"
                          value={form.email}
                          onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                          className={INPUT_CLS}
                        />
                      </label>

                      <label className={LABEL_CLS}>
                        Other addresses
                        <textarea
                          value={form.email_aliases}
                          onChange={(e) => setForm((f) => ({ ...f, email_aliases: e.target.value }))}
                          rows={2}
                          placeholder="anna.personal@gmail.com"
                          className={`${INPUT_CLS} h-auto py-2.5`}
                        />
                        <span className={HINT_CLS}>
                          One per line. Mail from these reaches them, and they can be emailed
                          here — but only the address above signs in.
                        </span>
                      </label>

                      <label className={LABEL_CLS}>
                        Slack user ID
                        <input
                          value={form.slack_user_id}
                          onChange={(e) => setForm((f) => ({ ...f, slack_user_id: e.target.value }))}
                          className={INPUT_CLS}
                          placeholder="U01ABC123"
                        />
                      </label>

                      <label className={LABEL_CLS}>
                        Discord user ID
                        <input
                          value={form.discord_user_id}
                          onChange={(e) => setForm((f) => ({ ...f, discord_user_id: e.target.value }))}
                          className={INPUT_CLS}
                          placeholder="123456789012345678"
                        />
                        <span className={HINT_CLS}>
                          Right-click your Discord username and &quot;Copy User ID&quot; (developer mode required).
                        </span>
                      </label>

                      <label className={LABEL_CLS}>
                        Telegram chat ID
                        <input
                          value={form.telegram_chat_id}
                          onChange={(e) => setForm((f) => ({ ...f, telegram_chat_id: e.target.value }))}
                          className={INPUT_CLS}
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
                      <label className={LABEL_CLS}>
                        On leave until
                        <input
                          type="date"
                          value={form.on_leave_until}
                          onChange={(e) => setForm((f) => ({ ...f, on_leave_until: e.target.value }))}
                          className={INPUT_CLS}
                        />
                      </label>
                    </DisclosureSection>
                  </div>
                ) : (
                  <dl className="divide-y divide-line">
                    {[
                      ["Kind", contact ? "Contact — outside the team" : "Team member"],
                      ["Preferred channel", person.preferred_channel],
                      ...(contact ? [] : [["Expected reply within", `${person.response_sla_hours} hours`]]),
                      ["Email", person.email ?? "—"],
                      ["Other addresses", (person.email_aliases ?? []).join(", ") || "—"],
                      ["Slack user ID", person.slack_user_id ?? "—"],
                      ["Discord user ID", person.discord_user_id ?? "—"],
                      ["Telegram chat ID", person.telegram_chat_id ?? "—"],
                      ["On leave until", person.on_leave_until ?? "—"],
                    ].map(([label, value]) => (
                      <div key={label} className="flex flex-col sm:flex-row sm:items-baseline gap-0.5 sm:gap-4 py-3">
                        <dt className="sm:w-48 flex-shrink-0 text-sm text-fg-muted">{label}</dt>
                        <dd className="text-[15px] text-fg break-words min-w-0">{value}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </section>

              {!person.archived && !contact && (
                <OpenLoopsSection
                  personId={personId}
                  canList={viewerIsPrincipal || viewerPersonId === personId}
                  canAssign={viewerPersonId !== null}
                />
              )}
                </>
              )}

              {/* Authority scope and availability — a contact approves nothing and is never chased */}
              {activeTab === "approvals" && !contactView && (
                <>
              <section>
                <h2 className={SECTION_TITLE_CLS}>What this person approves</h2>
                <p className={INTRO_CLS}>Proposals in these areas are routed to them for approval.</p>
                {editing ? (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {ALL_SCOPES.map(({ value, label, hint }) => {
                      const active = form.authority_scope.includes(value);
                      return (
                        <button
                          key={value}
                          type="button"
                          aria-pressed={active}
                          onClick={() => toggleScope(value)}
                          className={`px-3.5 py-2.5 rounded-xl border text-left transition-colors ${
                            active
                              ? "bg-accent/10 border-accent/60 text-fg"
                              : "bg-surface-elevated border-line text-fg-muted hover:border-line-strong"
                          }`}
                        >
                          <div className="text-[15px] font-medium">{label}</div>
                          <div className="text-[13px] leading-snug mt-0.5 text-fg-muted">{hint}</div>
                        </button>
                      );
                    })}
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {person.authority_scope.length === 0 ? (
                      <p className="text-[15px] text-fg-muted">No approval authority.</p>
                    ) : (
                      person.authority_scope.map((s) => {
                        const entry = ALL_SCOPES.find((x) => x.value === s);
                        return (
                          <span
                            key={s}
                            title={entry?.hint}
                            className={`inline-block px-3 py-1.5 rounded-full border text-sm font-medium ${
                              s === "wildcard"
                                ? "bg-accent/10 text-accent border-accent/30"
                                : "bg-surface-elevated text-fg border-line"
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

              <section className="mt-8">
                <div className="flex items-start justify-between gap-3">
                  <h2 className={SECTION_TITLE_CLS}>Availability windows</h2>
                  {editing && (
                    <Button onClick={addWindow} className="flex-shrink-0 -mt-1.5">
                      Add window
                    </Button>
                  )}
                </div>
                <div className="mb-4" />

                {editing ? (
                  <div className="space-y-3">
                    {form.availability.length === 0 && (
                      <p className="text-[15px] text-fg-muted">
                        No windows — person is considered always available.
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
                      <p className="text-[15px] text-fg-muted">Always available (no windows set).</p>
                    ) : (
                      <div className="space-y-2">
                        {person.availability.map((w, i) => (
                          <div
                            key={i}
                            className={`flex flex-wrap items-center gap-x-3 gap-y-2 ${ROW_CLS}`}
                          >
                            <div className="flex flex-wrap gap-1">
                              {w.weekdays.map((d) => (
                                <span key={d} className="px-2 py-0.5 rounded-lg bg-surface-overlay text-sm text-fg">
                                  {WEEKDAY_NAMES[d] ?? d}
                                </span>
                              ))}
                            </div>
                            <span className="text-fg">
                              {w.start_local} – {w.end_local}
                            </span>
                            <span className="text-fg-muted text-sm">{w.timezone}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </section>
                </>
              )}

              {activeTab === "respond" && <OutreachSection personId={personId} />}
              {activeTab === "style" && <WorkingStyleSection personId={personId} />}
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
