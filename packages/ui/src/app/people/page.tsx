"use client";

import Link from "next/link";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { useAskOEFormContext } from "@/components/askoe/AskOEContext";
import { TeamModeOffer } from "@/components/people/TeamModeOffer";
import Button from "@/components/ui/Button";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import SidePanel from "@/components/ui/SidePanel";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  createPerson,
  getPeopleViewer,
  listPeople,
  type PageFormField,
  type Person,
  type PersonKind,
} from "@/lib/api";
import {
  defaultKindForTab,
  defaultPeopleTab,
  effectiveKind,
  hiddenTeamCount,
  isContact,
  peopleForTab,
  personCardStatus,
  shouldOfferTeamMode,
  tabsFor,
  type PeopleTab,
} from "@/lib/peopleKinds";

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
const KINDS: PersonKind[] = ["team", "contact"];

// ---------------------------------------------------------------------------
// PersonCard — name, role, channel and one status. Approval scopes are on
// the person's own page.
// ---------------------------------------------------------------------------

const TONE_DOT = { ok: "bg-emerald-500", warn: "bg-amber-500", muted: "bg-fg-subtle" } as const;

function channelLabel(channel: string): string {
  return channel === "any" ? "Any channel" : channel.charAt(0).toUpperCase() + channel.slice(1);
}

function PersonCard({ person, today }: { person: Person; today: string }) {
  const contact = isContact(person);
  const status = personCardStatus(person, today);
  return (
    <Link
      href={`/people/${person.id}`}
      className="flex items-start gap-4 rounded-2xl border border-line bg-surface-elevated hover:bg-surface-hover hover:border-line-strong transition-colors p-5 group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
    >
      <div className="w-11 h-11 rounded-full bg-accent/10 text-accent flex items-center justify-center flex-shrink-0 text-base font-semibold">
        {person.full_name.charAt(0).toUpperCase()}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-base font-semibold text-fg group-hover:text-accent transition-colors truncate">
            {person.full_name}
          </span>
          {person.is_principal && (
            <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-accent/10 text-accent">
              Principal
            </span>
          )}
        </div>
        <div className="text-[15px] text-fg-muted mt-0.5 truncate">{person.role || "—"}</div>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-fg-muted">
          {!contact && <span>{channelLabel(person.preferred_channel)}</span>}
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className={`h-2 w-2 rounded-full ${TONE_DOT[status.tone]}`} />
            {status.label}
          </span>
        </div>
      </div>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Add Person modal
// ---------------------------------------------------------------------------

interface AddPersonModalProps {
  initialKind: PersonKind;
  /** Contacts are the principal's alone: nobody else is offered the choice. */
  canAddContacts: boolean;
  onCreated: (p: Person) => void;
  onClose: () => void;
}

const BLANK_FORM = {
  full_name: "",
  role: "",
  kind: "team" as PersonKind,
  is_principal: false,
  email: "",
  slack_user_id: "",
  telegram_chat_id: "",
  discord_user_id: "",
  preferred_channel: "any",
  response_sla_hours: "24",
  authority_scope: [] as string[],
};

const INPUT_CLS =
  "w-full h-11 px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";
const LABEL_CLS = "text-sm text-fg-muted flex flex-col gap-1.5";
const HINT_CLS = "text-[13px] text-fg-subtle mt-1";

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

// The approval scopes as toggles, each with what it routes to the person.
function ScopePicker({ selected, onToggle }: { selected: string[]; onToggle: (value: string) => void }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
      {ALL_SCOPES.map(({ value, label, hint }) => {
        const active = selected.includes(value);
        return (
          <button
            key={value}
            type="button"
            aria-pressed={active}
            onClick={() => onToggle(value)}
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
  );
}

const SCOPE_VALUES = ALL_SCOPES.map((s) => s.value);

function AddPersonModal({ initialKind, canAddContacts, onCreated, onClose }: AddPersonModalProps) {
  const [form, setForm] = useState({ ...BLANK_FORM, kind: canAddContacts ? initialKind : "team" });
  const kind = effectiveKind(form.kind, form.is_principal);
  const contact = kind === "contact";
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showContact, setShowContact] = useState(false);
  const [showAuthority, setShowAuthority] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // After the panel has taken focus on open, so it remembers the button
    // that opened it (and returns focus there on close).
    const id = requestAnimationFrame(() => nameRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, []);

  // Registered with Ask OE for the modal's lifetime — closing the modal
  // unregisters automatically (the hook's cleanup runs on unmount).
  const { suggestedCls, clearSuggested } = useAskOEFormContext({
    formId: "add_person",
    title: "Add person",
    description:
      "Adds a human the Executive coordinates with — a team member (can sign in, approve and be chased) or a contact (someone outside the team the Executive emails only when you ask). Authority scopes determine which proposals route to a team member for approval.",
    getFields: (): PageFormField[] => [
      { name: "full_name", label: "Full name", type: "text", value: form.full_name, required: true },
      ...(canAddContacts
        ? [{
            name: "kind",
            label: "Team member or contact",
            type: "select" as const,
            options: KINDS,
            value: form.kind,
            description: "team: works with you. contact: a client, contractor or advisor outside the team, private to you.",
          }]
        : []),
      { name: "role", label: contact ? "Role and company" : "Role", type: "text", value: form.role },
      {
        name: "is_principal",
        label: "This is me — Primary",
        type: "boolean",
        value: form.is_principal,
        description: "Marks the person as the primary decision-maker. Only for the user themselves.",
      },
      { name: "preferred_channel", label: "Preferred channel", type: "select", options: CHANNELS, value: form.preferred_channel },
      { name: "response_sla_hours", label: "Expected reply within (hours)", type: "number", value: Number(form.response_sla_hours) || 24 },
      { name: "email", label: "Email", type: "text", value: form.email },
      { name: "slack_user_id", label: "Slack user ID", type: "text", value: form.slack_user_id },
      { name: "discord_user_id", label: "Discord user ID", type: "text", value: form.discord_user_id },
      { name: "telegram_chat_id", label: "Telegram chat ID", type: "text", value: form.telegram_chat_id },
      {
        name: "authority_scope",
        label: "Approval authority",
        type: "json",
        value: form.authority_scope,
        description: `JSON array of scope tokens, each one of: ${SCOPE_VALUES.join(", ")}.`,
      },
    ],
    applyPatch: (values) => {
      const prior = { form, showContact, showAuthority };
      const applied: string[] = [];
      const skipped: string[] = [];
      const next = { ...form };
      for (const [key, raw] of Object.entries(values)) {
        switch (key) {
          case "full_name":
          case "role":
          case "email":
          case "slack_user_id":
          case "discord_user_id":
          case "telegram_chat_id":
            if (typeof raw !== "string") skipped.push(key);
            else { next[key] = raw; applied.push(key); }
            break;
          case "is_principal":
            if (typeof raw !== "boolean") skipped.push(key);
            else { next.is_principal = raw; applied.push(key); }
            break;
          case "kind":
            if (canAddContacts && (raw === "team" || raw === "contact")) { next.kind = raw; applied.push(key); }
            else skipped.push(key);
            break;
          case "preferred_channel":
            if (typeof raw === "string" && CHANNELS.includes(raw)) {
              next.preferred_channel = raw;
              applied.push(key);
            } else skipped.push(key);
            break;
          case "response_sla_hours": {
            const n = Number(raw);
            if (Number.isFinite(n) && n >= 1) {
              next.response_sla_hours = String(Math.round(n));
              applied.push(key);
            } else skipped.push(key);
            break;
          }
          case "authority_scope": {
            const arr = Array.isArray(raw)
              ? raw.filter((s): s is string => typeof s === "string" && SCOPE_VALUES.includes(s))
              : null;
            // Empty after filtering means no proposed scope was recognized —
            // skip rather than silently wiping every existing scope.
            if (arr !== null && arr.length > 0) { next.authority_scope = arr; applied.push(key); }
            else skipped.push(key);
            break;
          }
          default:
            skipped.push(key);
        }
      }
      setForm(next);
      // Open the disclosures so the suggested values are visible to review.
      if (applied.some((k) => ["slack_user_id", "discord_user_id", "telegram_chat_id", "preferred_channel", "response_sla_hours"].includes(k))) {
        setShowContact(true);
      }
      if (applied.includes("authority_scope")) setShowAuthority(true);
      return {
        applied,
        skipped,
        undo: () => {
          setForm(prior.form);
          setShowContact(prior.showContact);
          setShowAuthority(prior.showAuthority);
        },
      };
    },
  });

  function toggleScope(val: string) {
    setForm((f) => ({
      ...f,
      authority_scope: f.authority_scope.includes(val)
        ? f.authority_scope.filter((s) => s !== val)
        : [...f.authority_scope, val],
    }));
  }

  async function submit() {
    setSaving(true);
    setErr(null);
    try {
      const person = await createPerson({
        full_name: form.full_name.trim(),
        role: form.role.trim(),
        kind,
        is_principal: form.is_principal,
        email: form.email.trim() || null,
        slack_user_id: form.slack_user_id.trim() || null,
        telegram_chat_id: form.telegram_chat_id.trim() || null,
        discord_user_id: form.discord_user_id.trim() || null,
        preferred_channel: form.preferred_channel,
        response_sla_hours: Number(form.response_sla_hours) || 24,
        // A contact approves nothing; don't send scopes picked before switching.
        authority_scope: contact ? [] : form.authority_scope,
      });
      onCreated(person);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSaving(false);
    }
  }

  const title = contact ? "Add contact" : "Add person";
  return (
    <SidePanel
      open
      onClose={saving ? () => {} : onClose}
      title={title}
      footer={
        <div className="flex gap-2">
          <Button
            variant="primary"
            disabled={saving || !form.full_name.trim()}
            onClick={submit}
            className="flex-1"
          >
            {saving ? "Creating…" : title}
          </Button>
          <Button disabled={saving} onClick={onClose}>
            Cancel
          </Button>
        </div>
      }
    >
        <div className="space-y-4">
          {canAddContacts && !form.is_principal && (
            <div role="radiogroup" aria-label="Team member or contact" className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {KINDS.map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={form.kind === k}
                  onClick={() => { setForm((f) => ({ ...f, kind: k })); clearSuggested("kind"); }}
                  className={`px-4 py-3 rounded-xl border text-left transition-colors ${
                    form.kind === k
                      ? "bg-accent/10 border-accent/60 text-fg"
                      : "bg-surface-elevated border-line text-fg-muted hover:border-line-strong"
                  } ${suggestedCls("kind")}`}
                >
                  <div className="text-[15px] font-semibold">{k === "team" ? "Team member" : "Contact"}</div>
                  <div className="text-sm leading-snug mt-1 text-fg-muted">
                    {k === "team"
                      ? "Works with you: can sign in, message the Executive and approve."
                      : "Outside the team and private to you: emailed or invited only when you ask."}
                  </div>
                </button>
              ))}
            </div>
          )}

          {/* Always-visible: the 10-second path */}
          <label className={LABEL_CLS}>
            Full name *
            <input
              ref={nameRef}
              value={form.full_name}
              onChange={(e) => { const v = e.target.value; setForm((f) => ({ ...f, full_name: v })); clearSuggested("full_name"); }}
              className={`${INPUT_CLS} ${suggestedCls("full_name")}`}
              placeholder="Sarah Chen"
            />
          </label>

          <label className={LABEL_CLS}>
            {contact ? "Role and company" : "Role"}
            <input
              value={form.role}
              onChange={(e) => { const v = e.target.value; setForm((f) => ({ ...f, role: v })); clearSuggested("role"); }}
              className={`${INPUT_CLS} ${suggestedCls("role")}`}
              placeholder={contact ? "Head of Procurement, Acme" : "CFO (fractional)"}
            />
          </label>

          <label className={LABEL_CLS}>
            Email
            <input
              type="email"
              value={form.email}
              onChange={(e) => { const v = e.target.value; setForm((f) => ({ ...f, email: v })); clearSuggested("email"); }}
              className={`${INPUT_CLS} ${suggestedCls("email")}`}
              placeholder={contact ? "jordan@acme.example" : "sarah@example.com"}
            />
          </label>

          {!contact && (
          <label className="flex items-start gap-3 cursor-pointer select-none rounded-xl border border-line px-4 py-3">
            <input
              type="checkbox"
              checked={form.is_principal}
              onChange={(e) => {
                const checked = e.target.checked;
                setForm((f) => ({ ...f, is_principal: checked }));
                if (checked) setShowAuthority(true);
              }}
              className="mt-0.5 w-5 h-5 rounded accent-indigo-500 flex-shrink-0"
            />
            <span>
              <span className="block text-[15px] font-medium text-fg">This is me — Primary</span>
              <span className="block text-sm text-fg-muted">Marks you as the primary decision-maker.</span>
            </span>
          </label>
          )}

          {/* Contact & routing */}
          <DisclosureSection
            label={contact ? "Chat IDs" : "Contact & routing"}
            open={showContact}
            onToggle={() => setShowContact((v) => !v)}
          >
            {!contact && (
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
            </div>
            )}

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

          {/* Approval authority — a contact approves nothing */}
          {!contact && (
          <DisclosureSection
            label="Approval authority"
            open={showAuthority}
            onToggle={() => setShowAuthority((v) => !v)}
          >
            <div className="text-sm text-fg-muted">What this person approves</div>
            <ScopePicker selected={form.authority_scope} onToggle={toggleScope} />
          </DisclosureSection>
          )}

        </div>

        {err && <p className="text-sm text-rose-500 mt-4">{err}</p>}
    </SidePanel>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

const TAB_COPY: Record<PeopleTab, { label: string; blurb: string; empty: string; add: string }> = {
  team: {
    label: "Team",
    blurb: "People who work with you. They can sign in, message the Executive and approve what their authority covers.",
    empty: "No team members yet.",
    add: "Add person",
  },
  contacts: {
    label: "Contacts",
    blurb: "Clients, contractors and advisors outside the team — private to you. The Executive emails or invites them only when you ask it to; they can't sign in or message it, and nobody else on the team sees them.",
    empty: "No contacts yet.",
    add: "Add contact",
  },
};

export default function PeoplePage() {
  const { mode, loading: workspaceLoading } = useWorkspace();
  const [people, setPeople] = useState<Person[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [tab, setTab] = useState<PeopleTab | null>(null);
  const [offerFor, setOfferFor] = useState<string | null>(null);
  // Contacts are private to the principal. Until the viewer is known (or if
  // the check fails) nobody is offered them; the API enforces it regardless.
  const [viewerIsPrincipal, setViewerIsPrincipal] = useState(false);
  const [viewerLoading, setViewerLoading] = useState(true);

  const tabs = tabsFor(viewerIsPrincipal);
  // Open on the mode's default tab once the mode is known; a tab the user
  // picked is kept from then on (and never one this viewer is not offered).
  const activeTab: PeopleTab =
    tab !== null && tabs.includes(tab) ? tab : defaultPeopleTab(mode, viewerIsPrincipal);

  function refresh() {
    setLoading(true);
    listPeople({ includeContacts: true })
      .then(setPeople)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    refresh();
    getPeopleViewer()
      .then((v) => setViewerIsPrincipal(v.is_principal))
      .catch(() => setViewerIsPrincipal(false))
      .finally(() => setViewerLoading(false));
  }, []);

  const shown = peopleForTab(people, activeTab, mode);
  const hidden = activeTab === "team" ? hiddenTeamCount(people, mode) : 0;
  const copy = TAB_COPY[activeTab];
  const tabsId = useId();
  // Local date (YYYY-MM-DD) for "on leave until", sampled once per mount.
  const [today] = useState(() => new Date().toLocaleDateString("en-CA"));

  return (
    <div className="flex flex-col h-full bg-surface">
      {showAdd && (
        <AddPersonModal
          initialKind={defaultKindForTab(activeTab)}
          canAddContacts={viewerIsPrincipal}
          onCreated={(p) => {
            setPeople((prev) => [...prev, p]);
            setShowAdd(false);
            // Show the new row where it lives.
            setTab(isContact(p) ? "contacts" : "team");
            if (shouldOfferTeamMode(mode, p.kind ?? "team", p.is_principal)) setOfferFor(p.full_name);
          }}
          onClose={() => setShowAdd(false)}
        />
      )}
      {offerFor && <TeamModeOffer name={offerFor} onDone={() => setOfferFor(null)} />}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-6">
            <div className="min-w-0">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">People</h1>
              <p className="text-[15px] text-fg-muted mt-2 max-w-2xl">{copy.blurb}</p>
            </div>
            <Button variant="primary" onClick={() => setShowAdd(true)} className="flex-shrink-0 self-start">
              {copy.add}
            </Button>
          </div>

          {tabs.length > 1 && (
            <div className="mb-6">
              <SectionTabs
                idBase={tabsId}
                label="Team or contacts"
                active={activeTab}
                onChange={setTab}
                disabled={(workspaceLoading || viewerLoading) && tab === null}
                tabs={tabs.map((t) => ({
                  id: t,
                  label: TAB_COPY[t].label,
                  count: loading ? undefined : peopleForTab(people, t, mode).length,
                }))}
              />
            </div>
          )}

          <div {...(tabs.length > 1 ? sectionPanelProps(tabsId, activeTab) : {})}>
          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
              {error}
            </div>
          )}
          {!loading && !error && shown.length === 0 && (
            <div className="rounded-2xl border border-line bg-surface-elevated p-10 text-center">
              <p className="text-fg-muted text-[15px] mb-5">{copy.empty}</p>
              <Button variant="primary" onClick={() => setShowAdd(true)}>
                {activeTab === "contacts" ? "Add your first contact" : "Add your first person"}
              </Button>
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {shown.map((person) => (
              <PersonCard key={person.id} person={person} today={today} />
            ))}
          </div>

          {hidden > 0 && (
            <p className="text-sm text-fg-muted mt-5">
              {hidden === 1 ? "1 other team member is" : `${hidden} other team members are`} hidden while you
              use Open Executive just for yourself. Switch to team mode in Settings to see them.
            </p>
          )}
          </div>
        </div>
      </main>
    </div>
  );
}
