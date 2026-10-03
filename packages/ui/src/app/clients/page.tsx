"use client";

import { useCallback, useEffect, useId, useState } from "react";

import Link from "next/link";

import Button, { buttonClass } from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import SidePanel from "@/components/ui/SidePanel";
import {
  activateClient,
  type ClientCockpitCard,
  type ClientDraftResult,
  type ClientMetaPatch,
  type ClientsStatus,
  createClient,
  createClientFromDraft,
  deleteClient,
  generateClientDraft,
  getClientsCockpit,
  listClients,
  saveActiveClient,
  updateClientMeta,
} from "@/lib/api";
import { clientCountsSummary, renewalBadge } from "@/lib/practice";

// Client-company switcher for fractional / multi-client use. One client is
// live at a time; switching saves the current client back to its slot and
// restores the target. Single-company installs see only the intro + create
// form — nothing about the default experience changes until a client exists.

const INPUT_CLS =
  "w-full px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";
const FIELD_CLS = `${INPUT_CLS} h-11`;
const LABEL_CLS = "text-sm text-fg-muted flex flex-col gap-1.5";

// base64url-encode a prefill payload for the /jobs/{name} runner page
// (mirrors decodePrefill there).
function encodePrefill(payload: Record<string, string>): string {
  const json = JSON.stringify(payload);
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export default function ClientsPage() {
  const [status, setStatus] = useState<ClientsStatus>({
    active: null,
    fixture_active: null,
    clients: [],
  });
  const [loading, setLoading] = useState(true);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [toast, setToast] = useState<{ message: string; kind: "success" | "error" } | null>(null);
  const [name, setName] = useState("");
  const [source, setSource] = useState<"current" | "blank">("current");
  const [creating, setCreating] = useState(false);
  // The "New client" panel and which of its two ways is showing.
  const [createOpen, setCreateOpen] = useState(false);
  const [createWay, setCreateWay] = useState<"company" | "notes">("company");
  const createTabsId = useId();

  // Engagement-intake (AI draft) flow.
  const [notes, setNotes] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [generating, setGenerating] = useState(false);
  const [draft, setDraft] = useState<ClientDraftResult | null>(null);
  const [draftName, setDraftName] = useState("");
  const [creatingDraft, setCreatingDraft] = useState(false);

  // Practice cockpit (multi-client only) + per-slot engagement metadata edit.
  const [cockpit, setCockpit] = useState<ClientCockpitCard[]>([]);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [metaForm, setMetaForm] = useState<ClientMetaPatch>({});
  const [savingMeta, setSavingMeta] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = await listClients();
      setStatus(next);
      if (next.clients.length >= 2) {
        try {
          setCockpit((await getClientsCockpit()).clients);
        } catch {
          setCockpit([]);
        }
      } else {
        setCockpit([]);
      }
    } catch {
      setToast({ message: "Failed to load clients", kind: "error" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(t);
  }, [toast]);

  async function handleCreate() {
    if (!name.trim()) return;
    setCreating(true);
    try {
      const created = await createClient(name.trim(), source);
      setToast({
        message: created.active
          ? `${created.display_name} created from your current company and is now active.`
          : `${created.display_name} created — activate it to start onboarding.`,
        kind: "success",
      });
      setName("");
      setCreateOpen(false);
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Create failed", kind: "error" });
    } finally {
      setCreating(false);
    }
  }

  function openMetaEditor(slug: string) {
    const c = status.clients.find((x) => x.slug === slug) as
      | (Record<string, unknown> & { slug: string })
      | undefined;
    setMetaForm({
      role: (c?.role as string) ?? "",
      status: (c?.status as string) ?? "active",
      renewal_date: (c?.renewal_date as string) ?? "",
      retainer: (c?.retainer as string) ?? "",
      primary_contact: (c?.primary_contact as string) ?? "",
      notes: (c?.notes as string) ?? "",
    });
    setEditingSlug(slug);
  }

  async function handleSaveMeta() {
    if (!editingSlug) return;
    setSavingMeta(true);
    try {
      // Drop empty strings so we never overwrite with blanks unintentionally.
      const patch = Object.fromEntries(
        Object.entries(metaForm).filter(([, v]) => v !== "" && v !== undefined),
      ) as ClientMetaPatch;
      await updateClientMeta(editingSlug, patch);
      setToast({ message: "Engagement details saved.", kind: "success" });
      setEditingSlug(null);
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Save failed", kind: "error" });
    } finally {
      setSavingMeta(false);
    }
  }

  async function handleGenerateDraft() {
    if (!notes.trim() && attachments.length === 0) return;
    setGenerating(true);
    try {
      const result = await generateClientDraft(notes.trim(), attachments);
      setDraft(result);
      setDraftName(result.display_name);
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Draft failed", kind: "error" });
    } finally {
      setGenerating(false);
    }
  }

  async function handleCreateFromDraft(activate: boolean) {
    if (!draft) return;
    setCreatingDraft(true);
    try {
      const displayName = draftName.trim() || draft.display_name;
      const bundle = { ...draft.bundle, profile: { ...draft.bundle.profile, name: displayName } };
      const created = await createClientFromDraft(displayName, bundle, notes.trim());
      if (activate) {
        await activateClient(created.slug);
        setToast({ message: `${displayName} created and activated.`, kind: "success" });
      } else {
        setToast({
          message: `${displayName} created — activate it to start the engagement.`,
          kind: "success",
        });
      }
      setDraft(null);
      setNotes("");
      setAttachments([]);
      setCreateOpen(false);
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Create failed", kind: "error" });
    } finally {
      setCreatingDraft(false);
    }
  }

  async function handleActivate(slug: string) {
    setBusySlug(slug);
    try {
      const result = await activateClient(slug);
      setToast({
        message: result.mcp_config_changed
          ? `Switched to ${slug}. MCP tool config changed — it applies on the next API restart.`
          : `Switched to ${slug}.`,
        kind: "success",
      });
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Switch failed", kind: "error" });
    } finally {
      setBusySlug(null);
    }
  }

  async function handleSave() {
    setBusySlug(status.active);
    try {
      const result = await saveActiveClient();
      setToast({ message: `Saved ${result.slug} to its slot.`, kind: "success" });
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Save failed", kind: "error" });
    } finally {
      setBusySlug(null);
    }
  }

  async function handleDelete(slug: string) {
    setBusySlug(slug);
    try {
      await deleteClient(slug);
      setToast({ message: `Deleted ${slug}.`, kind: "success" });
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Delete failed", kind: "error" });
    } finally {
      setBusySlug(null);
    }
  }

  const editingClient = status.clients.find((c) => c.slug === editingSlug) ?? null;
  const fixtureActive = !!status.fixture_active;

  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-4xl mx-auto px-4 sm:px-6 py-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Client companies</h1>
            <p className="mt-2 text-[15px] text-fg-muted">
              Run several client companies from one Open Executive — one active at a
              time. Switching saves the current client&apos;s full state (chat,
              schedule, documents, MCP tools) to its slot and
              restores the target.
            </p>
          </div>
          <Button variant="primary" onClick={() => setCreateOpen(true)} className="flex-shrink-0 self-start">
            New client
          </Button>
        </div>

        {toast && (
          <div
            role="status"
            className={`mt-5 rounded-xl border px-4 py-3 text-[15px] ${
              toast.kind === "success"
                ? "border-line bg-surface-elevated text-fg"
                : "border-red-500/40 bg-red-500/10 text-red-500"
            }`}
          >
            {toast.message}
          </div>
        )}

        {status.rotation_in_progress && (
          <div className="mt-5 rounded-xl border border-line bg-surface-elevated px-4 py-3 text-[15px] text-fg-muted">
            Overnight rotation is running — the active client will switch
            briefly while parked clients are refreshed, then return.
          </div>
        )}

        {status.fixture_active && (
          <div className="mt-5 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-[15px] text-amber-700 dark:text-amber-400">
            Demo fixture <strong>{status.fixture_active}</strong> is active —
            unload it on the Company Simulator page before working with clients.
          </div>
        )}

        {/* Practice cockpit — only in multi-client mode (2+ slots) */}
        {cockpit.length >= 2 && (
          <section className="mt-8">
            <h2 className="text-lg font-semibold text-fg">Practice cockpit</h2>
            <p className="mt-1 text-[15px] text-fg-muted">
              All clients at a glance. Parked clients show their state as of the
              last save point.
            </p>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {cockpit.map((c) => (
                <div
                  key={`cockpit-${c.slug}`}
                  className={`rounded-2xl border p-4 ${
                    c.is_active ? "border-line-strong bg-surface-overlay" : "border-line bg-surface-elevated"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-[15px] font-semibold text-fg truncate">
                      {c.display_name}
                      {c.role ? (
                        <span className="text-fg-muted font-normal"> · {c.role}</span>
                      ) : null}
                    </div>
                    {c.is_active ? (
                      <span className="text-xs font-medium px-2 py-0.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 flex-shrink-0">
                        active
                      </span>
                    ) : (
                      (() => {
                        const badge = renewalBadge(c.days_to_renewal);
                        return badge ? (
                          <span
                            className={`text-xs font-medium px-2 py-0.5 rounded-full border flex-shrink-0 ${
                              badge.urgent
                                ? "border-red-500/40 bg-red-500/10 text-red-500"
                                : "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
                            }`}
                          >
                            {badge.label}
                          </span>
                        ) : null;
                      })()
                    )}
                  </div>
                  <div className="mt-1.5 text-sm text-fg-muted">
                    {clientCountsSummary(c)}
                  </div>
                  {c.has_state && (
                    <div className="mt-3">
                      <Link
                        href={`/jobs/engagement_value_report?prefill=${encodePrefill({ client_slug: c.slug })}`}
                        className={buttonClass("secondary", "sm")}
                      >
                        Value report
                      </Link>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}

        {/* List */}
        {loading ? (
          <p className="mt-8 text-[15px] text-fg-muted">Loading clients…</p>
        ) : status.clients.length === 0 ? (
          <div className="mt-8 rounded-2xl border border-line bg-surface-elevated p-8 text-center">
            <p className="text-[15px] text-fg-muted">
              No clients yet. Create one from your current company to enter
              multi-client mode — single-company use is unaffected until you do.
            </p>
          </div>
        ) : (
          <section className="mt-8">
            {cockpit.length >= 2 && <h2 className="text-lg font-semibold text-fg mb-3">All clients</h2>}
            <div className="space-y-3">
            {status.clients.map((c) => {
              const isActive = c.slug === status.active;
              const busy = busySlug === c.slug;
              return (
                <div
                  key={c.slug}
                  className={`rounded-2xl border p-5 ${
                    isActive
                      ? "border-line-strong bg-surface-overlay"
                      : "border-line bg-surface-elevated"
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="text-base font-semibold text-fg truncate">
                          {c.display_name}
                        </h3>
                        {isActive && (
                          <span className="text-xs font-medium px-2 py-0.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                            active
                          </span>
                        )}
                        {c.has_mcp_config && (
                          <span className="text-xs font-medium px-2 py-0.5 rounded-full border border-line text-fg-muted">
                            MCP tools
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-fg-muted">
                        {[c.role, c.status, c.industry, c.stage]
                          .filter(Boolean)
                          .join(" · ") || c.slug}
                        {" · "}
                        {c.doc_count} doc{c.doc_count !== 1 ? "s" : ""}
                        {c.saved_at
                          ? ` · saved ${new Date(c.saved_at).toLocaleString()}`
                          : " · never saved"}
                      </p>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {isActive ? (
                        <Button
                          onClick={() => void handleSave()}
                          disabled={busy || fixtureActive}
                        >
                          {busy ? "Saving…" : "Save now"}
                        </Button>
                      ) : (
                        <Button
                          variant="primary"
                          onClick={() => void handleActivate(c.slug)}
                          disabled={busy || fixtureActive}
                        >
                          {busy ? "Switching…" : "Activate"}
                        </Button>
                      )}
                      <OverflowMenu
                        label={`More actions for ${c.display_name}`}
                        items={[
                          { label: "Engagement details", onSelect: () => openMetaEditor(c.slug) },
                          ...(isActive
                            ? []
                            : [{
                                label: "Delete client",
                                danger: true,
                                disabled: busy,
                                onSelect: () => {
                                  if (window.confirm(`Delete ${c.display_name}? Its saved slot is removed and this cannot be undone.`)) {
                                    void handleDelete(c.slug);
                                  }
                                },
                              }]),
                        ]}
                      />
                    </div>
                  </div>
                </div>
              );
            })}
            </div>
          </section>
        )}

        <p className="mt-8 text-sm text-fg-muted leading-relaxed">
          Only the active client is live — its scheduled actions fire and its
          documents are indexed; parked clients sleep in their slots. Your
          original company is preserved automatically the first time you
          switch, and can be restored from the Company Simulator page.
        </p>
      </div>

      {/* New client: from a company, or drafted from intake notes */}
      <SidePanel
        open={createOpen}
        onClose={() => {
          if (!creating && !generating && !creatingDraft) setCreateOpen(false);
        }}
        title="New client"
        width="lg"
      >
        {toast?.kind === "error" && (
          <div role="alert" className="mb-4 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-[15px] text-red-500">
            {toast.message}
          </div>
        )}
        <div className="mb-5">
          <SectionTabs
            idBase={createTabsId}
            label="How to create the client"
            tabs={[
              { id: "company", label: "From a company" },
              { id: "notes", label: "From intake notes" },
            ]}
            active={createWay}
            onChange={setCreateWay}
          />
        </div>
        <div {...sectionPanelProps(createTabsId, createWay)}>
          {createWay === "company" ? (
            <div className="space-y-4">
              <label className={LABEL_CLS}>
                Client company name
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && name.trim() && !creating && !fixtureActive) void handleCreate();
                  }}
                  placeholder="Client company name"
                  className={FIELD_CLS}
                />
              </label>
              <label className={LABEL_CLS}>
                Start from
                <select
                  value={source}
                  onChange={(e) => setSource(e.target.value as "current" | "blank")}
                  className={FIELD_CLS}
                >
                  <option value="current">From current company</option>
                  <option value="blank">Blank (onboard fresh)</option>
                </select>
              </label>
              <p className="text-sm text-fg-muted">
                {source === "current"
                  ? "Captures the live company into the new client slot and makes it the active client."
                  : "Creates an empty client. Activate it, then run company onboarding and upload its documents."}
              </p>
              <Button
                variant="primary"
                onClick={() => void handleCreate()}
                disabled={creating || !name.trim() || fixtureActive}
              >
                {creating ? "Creating…" : "Create"}
              </Button>
            </div>
          ) : (
            <div>
              <p className="text-[15px] text-fg-muted">
                Paste real intake material — call notes, a brief, website copy — or
                attach PDFs, Word, Excel, or CSV files, and the AI drafts the
                client&apos;s profile, org, starter documents, and known history.
                Attachments are also saved as company documents. It extracts only
                what the material says: unknowns stay blank and become open
                questions, and no contact details are ever imported.
              </p>

              {!draft ? (
                <>
                  <textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    rows={7}
                    aria-label="Intake notes"
                    placeholder="e.g. Notes from the kickoff call with Meridian Solar: 80-person commercial solar installer in Texas, CEO Dana Reyes, struggling with project-margin visibility…"
                    className={`mt-4 ${INPUT_CLS} py-2.5`}
                  />

                  <div className="mt-3 flex flex-wrap items-center gap-3">
                    <label className={`${buttonClass("secondary", "md")} cursor-pointer focus-within:ring-2 focus-within:ring-accent/60`}>
                      Attach files
                      <input
                        type="file"
                        multiple
                        accept=".pdf,.docx,.doc,.xlsx,.xlsm,.csv,.md,.txt"
                        className="sr-only"
                        onChange={(e) => {
                          const picked = Array.from(e.target.files ?? []);
                          if (picked.length) setAttachments((prev) => [...prev, ...picked]);
                          e.target.value = "";
                        }}
                      />
                    </label>
                    <span className="text-sm text-fg-muted">
                      PDF, Word, Excel, CSV, or text
                    </span>
                  </div>

                  {attachments.length > 0 && (
                    <ul className="mt-3 flex flex-col gap-1.5">
                      {attachments.map((f, i) => (
                        <li
                          key={`${f.name}-${i}`}
                          className="flex items-center justify-between gap-2 rounded-xl border border-line bg-surface pl-4 pr-1 py-1 text-sm text-fg"
                        >
                          <span className="truncate">{f.name}</span>
                          <button
                            type="button"
                            onClick={() =>
                              setAttachments((prev) => prev.filter((_, j) => j !== i))
                            }
                            aria-label={`Remove ${f.name}`}
                            className="shrink-0 inline-flex h-10 w-10 items-center justify-center rounded-lg text-lg text-fg-muted hover:text-fg hover:bg-surface-overlay"
                          >
                            ×
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  <Button
                    variant="primary"
                    onClick={() => void handleGenerateDraft()}
                    disabled={
                      generating ||
                      (!notes.trim() && attachments.length === 0) ||
                      fixtureActive
                    }
                    className="mt-4"
                  >
                    {generating ? "Drafting…" : "Draft client"}
                  </Button>
                </>
              ) : (
                <div className="mt-4">
                  <label className={LABEL_CLS}>
                    Client name
                    <input
                      value={draftName}
                      onChange={(e) => setDraftName(e.target.value)}
                      className={FIELD_CLS}
                    />
                  </label>
                  <p className="mt-2 text-sm text-fg-muted">
                    {draft.bundle.people.length} people · {draft.bundle.departments.length} departments · {draft.bundle.docs.length} docs
                  </p>
                  {draft.bundle.people.length > 0 && (
                    <p className="mt-2 text-sm text-fg-muted">
                      Roster:{" "}
                      {draft.bundle.people
                        .map((p) => `${p.full_name}${p.is_principal ? " (principal)" : ""}`)
                        .join(", ")}
                    </p>
                  )}
                  {draft.bundle.docs.length > 0 && (
                    <p className="mt-1 text-sm text-fg-muted">
                      Docs: {draft.bundle.docs.map((d) => d.filename).join(", ")}
                    </p>
                  )}
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button
                      variant="primary"
                      onClick={() => void handleCreateFromDraft(true)}
                      disabled={creatingDraft || !draftName.trim()}
                    >
                      {creatingDraft ? "Creating…" : "Create & activate"}
                    </Button>
                    <Button
                      onClick={() => void handleCreateFromDraft(false)}
                      disabled={creatingDraft || !draftName.trim()}
                    >
                      {creatingDraft ? "Creating…" : "Create client"}
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => setDraft(null)}
                      disabled={creatingDraft}
                    >
                      ← Edit notes
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </SidePanel>

      {/* Engagement details for one client */}
      <SidePanel
        open={editingClient !== null}
        onClose={() => {
          if (!savingMeta) setEditingSlug(null);
        }}
        title="Engagement details"
        subtitle={editingClient?.display_name}
        footer={
          <div className="flex gap-2">
            <Button variant="primary" onClick={() => void handleSaveMeta()} disabled={savingMeta} className="flex-1 sm:flex-none">
              {savingMeta ? "Saving…" : "Save details"}
            </Button>
            <Button onClick={() => setEditingSlug(null)} disabled={savingMeta}>
              Cancel
            </Button>
          </div>
        }
      >
        {toast?.kind === "error" && (
          <div role="alert" className="mb-4 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-[15px] text-red-500">
            {toast.message}
          </div>
        )}
        <div className="space-y-4">
          <label className={LABEL_CLS}>
            Your role
            <input
              value={metaForm.role ?? ""}
              onChange={(e) => setMetaForm({ ...metaForm, role: e.target.value })}
              placeholder="Your role (e.g. Fractional CFO)"
              className={FIELD_CLS}
            />
          </label>
          <label className={LABEL_CLS}>
            Status
            <select
              value={metaForm.status ?? "active"}
              onChange={(e) => setMetaForm({ ...metaForm, status: e.target.value })}
              className={FIELD_CLS}
            >
              <option value="active">active</option>
              <option value="paused">paused</option>
              <option value="winding_down">winding down</option>
              <option value="completed">completed</option>
            </select>
          </label>
          <label className={LABEL_CLS}>
            Renewal
            <input
              type="date"
              value={metaForm.renewal_date ?? ""}
              onChange={(e) =>
                setMetaForm({ ...metaForm, renewal_date: e.target.value })
              }
              className={FIELD_CLS}
            />
          </label>
          <label className={LABEL_CLS}>
            Retainer
            <input
              value={metaForm.retainer ?? ""}
              onChange={(e) => setMetaForm({ ...metaForm, retainer: e.target.value })}
              placeholder="Retainer (display only)"
              className={FIELD_CLS}
            />
          </label>
          <label className={LABEL_CLS}>
            Primary contact
            <input
              value={metaForm.primary_contact ?? ""}
              onChange={(e) =>
                setMetaForm({ ...metaForm, primary_contact: e.target.value })
              }
              placeholder="Primary contact"
              className={FIELD_CLS}
            />
          </label>
          <label className={LABEL_CLS}>
            Notes
            <input
              value={metaForm.notes ?? ""}
              onChange={(e) => setMetaForm({ ...metaForm, notes: e.target.value })}
              placeholder="Notes"
              className={FIELD_CLS}
            />
          </label>
        </div>
      </SidePanel>
    </main>
  );
}
