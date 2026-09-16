"use client";

import { useCallback, useEffect, useState } from "react";

import Link from "next/link";

import Icon from "@/components/Icon";
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
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

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
      setConfirmDelete(null);
      await refresh();
    } catch (e: unknown) {
      setToast({ message: e instanceof Error ? e.message : "Delete failed", kind: "error" });
    } finally {
      setBusySlug(null);
    }
  }

  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
        <h1 className="text-xl font-semibold text-fg">Client companies</h1>
        <p className="mt-1 text-sm text-fg-muted">
          Run several client companies from one Open Executive — one active at a
          time. Switching saves the current client&apos;s full state (chat,
          schedule, documents, MCP tools) to its slot and
          restores the target.
        </p>

        {toast && (
          <div
            className={`mt-4 rounded-lg border px-3 py-2 text-sm ${
              toast.kind === "success"
                ? "border-line bg-surface-elevated text-fg"
                : "border-red-500/40 bg-red-500/10 text-red-400"
            }`}
          >
            {toast.message}
          </div>
        )}

        {status.rotation_in_progress && (
          <div className="mt-4 rounded-lg border border-line bg-surface-elevated px-3 py-2 text-sm text-fg-muted">
            Overnight rotation is running — the active client will switch
            briefly while parked clients are refreshed, then return.
          </div>
        )}

        {status.fixture_active && (
          <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-400">
            Demo fixture <strong>{status.fixture_active}</strong> is active —
            unload it on the Company Simulator page before working with clients.
          </div>
        )}

        {/* Practice cockpit — only in multi-client mode (2+ slots) */}
        {cockpit.length >= 2 && (
          <div className="mt-6 rounded-xl border border-line bg-surface-elevated p-4">
            <h2 className="text-sm font-medium text-fg">Practice cockpit</h2>
            <p className="mt-1 text-xs text-fg-muted">
              All clients at a glance. Parked clients show their state as of the
              last save point.
            </p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {cockpit.map((c) => (
                <div
                  key={`cockpit-${c.slug}`}
                  className={`rounded-lg border p-3 ${
                    c.is_active ? "border-line-strong bg-surface-overlay" : "border-line"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-xs font-medium text-fg truncate">
                      {c.display_name}
                      {c.role ? (
                        <span className="text-fg-muted font-normal"> · {c.role}</span>
                      ) : null}
                    </div>
                    {c.is_active ? (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-400 flex-shrink-0">
                        active
                      </span>
                    ) : (
                      (() => {
                        const badge = renewalBadge(c.days_to_renewal);
                        return badge ? (
                          <span
                            className={`text-[10px] px-1.5 py-0.5 rounded-full border flex-shrink-0 ${
                              badge.urgent
                                ? "border-red-500/40 bg-red-500/10 text-red-400"
                                : "border-amber-500/40 bg-amber-500/10 text-amber-400"
                            }`}
                          >
                            {badge.label}
                          </span>
                        ) : null;
                      })()
                    )}
                  </div>
                  <div className="mt-1 text-[11px] text-fg-muted">
                    {clientCountsSummary(c)}
                  </div>
                  {c.has_state && (
                    <div className="mt-1.5">
                      <Link
                        href={`/jobs/engagement_value_report?prefill=${encodePrefill({ client_slug: c.slug })}`}
                        className="text-[11px] text-indigo-400 hover:text-indigo-300"
                      >
                        Value report →
                      </Link>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Create */}
        <div className="mt-6 rounded-xl border border-line bg-surface-elevated p-4">
          <h2 className="text-sm font-medium text-fg">New client</h2>
          <div className="mt-3 flex flex-col sm:flex-row gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Client company name"
              className="flex-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-muted focus:outline-none focus:border-line-strong"
            />
            <select
              value={source}
              onChange={(e) => setSource(e.target.value as "current" | "blank")}
              className="rounded-lg border border-line bg-surface px-3 py-2 text-sm text-fg focus:outline-none"
            >
              <option value="current">From current company</option>
              <option value="blank">Blank (onboard fresh)</option>
            </select>
            <button
              onClick={() => void handleCreate()}
              disabled={creating || !name.trim() || !!status.fixture_active}
              className="rounded-lg border border-line bg-surface-overlay px-4 py-2 text-sm font-medium text-fg hover:border-line-strong disabled:opacity-50"
            >
              {creating ? "Creating…" : "Create"}
            </button>
          </div>
          <p className="mt-2 text-xs text-fg-muted">
            {source === "current"
              ? "Captures the live company into the new client slot and makes it the active client."
              : "Creates an empty client. Activate it, then run company onboarding and upload its documents."}
          </p>
        </div>

        {/* New engagement from intake notes (AI) */}
        <div className="mt-4 rounded-xl border border-line bg-surface-elevated p-4">
          <h2 className="text-sm font-medium text-fg">New client from intake notes</h2>
          <p className="mt-1 text-xs text-fg-muted">
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
                rows={5}
                placeholder="e.g. Notes from the kickoff call with Meridian Solar: 80-person commercial solar installer in Texas, CEO Dana Reyes, struggling with project-margin visibility…"
                className="mt-3 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-muted focus:outline-none focus:border-line-strong"
              />

              <div className="mt-2 flex flex-wrap items-center gap-2">
                <label className="cursor-pointer rounded-lg border border-line bg-surface-overlay px-3 py-1.5 text-xs font-medium text-fg hover:border-line-strong">
                  Attach files
                  <input
                    type="file"
                    multiple
                    accept=".pdf,.docx,.doc,.xlsx,.xlsm,.csv,.md,.txt"
                    className="hidden"
                    onChange={(e) => {
                      const picked = Array.from(e.target.files ?? []);
                      if (picked.length) setAttachments((prev) => [...prev, ...picked]);
                      e.target.value = "";
                    }}
                  />
                </label>
                <span className="text-xs text-fg-muted">
                  PDF, Word, Excel, CSV, or text
                </span>
              </div>

              {attachments.length > 0 && (
                <ul className="mt-2 flex flex-col gap-1">
                  {attachments.map((f, i) => (
                    <li
                      key={`${f.name}-${i}`}
                      className="flex items-center justify-between gap-2 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs text-fg"
                    >
                      <span className="truncate">{f.name}</span>
                      <button
                        type="button"
                        onClick={() =>
                          setAttachments((prev) => prev.filter((_, j) => j !== i))
                        }
                        aria-label={`Remove ${f.name}`}
                        className="shrink-0 text-fg-muted hover:text-fg"
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <button
                onClick={() => void handleGenerateDraft()}
                disabled={
                  generating ||
                  (!notes.trim() && attachments.length === 0) ||
                  !!status.fixture_active
                }
                className="mt-2 rounded-lg border border-line bg-surface-overlay px-4 py-2 text-sm font-medium text-fg hover:border-line-strong disabled:opacity-50"
              >
                {generating ? "Drafting…" : "Draft client"}
              </button>
            </>
          ) : (
            <div className="mt-3">
              <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
                <input
                  value={draftName}
                  onChange={(e) => setDraftName(e.target.value)}
                  className="flex-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm text-fg focus:outline-none focus:border-line-strong"
                />
                <span className="text-xs text-fg-muted">
                  {draft.bundle.people.length} people · {draft.bundle.departments.length} departments · {draft.bundle.docs.length} docs
                </span>
              </div>
              {draft.bundle.people.length > 0 && (
                <p className="mt-2 text-xs text-fg-muted">
                  Roster:{" "}
                  {draft.bundle.people
                    .map((p) => `${p.full_name}${p.is_principal ? " (principal)" : ""}`)
                    .join(", ")}
                </p>
              )}
              {draft.bundle.docs.length > 0 && (
                <p className="mt-1 text-xs text-fg-muted">
                  Docs: {draft.bundle.docs.map((d) => d.filename).join(", ")}
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => void handleCreateFromDraft(false)}
                  disabled={creatingDraft || !draftName.trim()}
                  className="rounded-lg border border-line bg-surface-overlay px-4 py-2 text-sm font-medium text-fg hover:border-line-strong disabled:opacity-50"
                >
                  {creatingDraft ? "Creating…" : "Create client"}
                </button>
                <button
                  onClick={() => void handleCreateFromDraft(true)}
                  disabled={creatingDraft || !draftName.trim()}
                  className="rounded-lg border border-line bg-surface-overlay px-4 py-2 text-sm font-medium text-fg hover:border-line-strong disabled:opacity-50"
                >
                  {creatingDraft ? "Creating…" : "Create & activate"}
                </button>
                <button
                  onClick={() => setDraft(null)}
                  disabled={creatingDraft}
                  className="rounded-lg border border-line px-4 py-2 text-sm text-fg-muted hover:border-line-strong disabled:opacity-50"
                >
                  ← Edit notes
                </button>
              </div>
            </div>
          )}
        </div>

        {/* List */}
        {loading ? (
          <p className="mt-6 text-sm text-fg-muted">Loading clients…</p>
        ) : status.clients.length === 0 ? (
          <p className="mt-6 text-sm text-fg-muted">
            No clients yet. Create one from your current company to enter
            multi-client mode — single-company use is unaffected until you do.
          </p>
        ) : (
          <div className="mt-6 space-y-3">
            {status.clients.map((c) => {
              const isActive = c.slug === status.active;
              const busy = busySlug === c.slug;
              return (
                <div
                  key={c.slug}
                  className={`rounded-xl border p-4 ${
                    isActive
                      ? "border-line-strong bg-surface-overlay"
                      : "border-line bg-surface-elevated"
                  }`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-medium text-fg truncate">
                          {c.display_name}
                        </h3>
                        {isActive && (
                          <span className="text-[10px] font-medium px-2 py-0.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-400">
                            active
                          </span>
                        )}
                        {c.has_mcp_config && (
                          <span className="text-[10px] font-medium px-2 py-0.5 rounded-full border border-line text-fg-muted">
                            MCP tools
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-fg-muted">
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
                    <div className="flex items-center gap-2 shrink-0">
                      {isActive ? (
                        <button
                          onClick={() => void handleSave()}
                          disabled={busy || !!status.fixture_active}
                          className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-fg hover:border-line-strong disabled:opacity-50"
                        >
                          {busy ? "Saving…" : "Save now"}
                        </button>
                      ) : (
                        <>
                          <button
                            onClick={() => void handleActivate(c.slug)}
                            disabled={busy || !!status.fixture_active}
                            className="rounded-lg border border-line bg-surface-overlay px-3 py-1.5 text-xs font-medium text-fg hover:border-line-strong disabled:opacity-50"
                          >
                            {busy ? "Switching…" : "Activate"}
                          </button>
                          {confirmDelete === c.slug ? (
                            <button
                              onClick={() => void handleDelete(c.slug)}
                              disabled={busy}
                              className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-400 disabled:opacity-50"
                            >
                              Confirm delete
                            </button>
                          ) : (
                            <button
                              onClick={() => setConfirmDelete(c.slug)}
                              disabled={busy}
                              aria-label={`Delete ${c.display_name}`}
                              className="rounded-lg border border-line px-2 py-1.5 text-xs text-fg-muted hover:border-line-strong disabled:opacity-50"
                            >
                              <Icon name="trash" size="w-3.5 h-3.5" />
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                  <div className="mt-2">
                    {editingSlug === c.slug ? (
                      <div className="rounded-lg border border-line bg-surface p-3 grid gap-2 sm:grid-cols-2">
                        <input
                          value={metaForm.role ?? ""}
                          onChange={(e) => setMetaForm({ ...metaForm, role: e.target.value })}
                          placeholder="Your role (e.g. Fractional CFO)"
                          className="rounded border border-line bg-surface-elevated px-2 py-1.5 text-xs text-fg placeholder:text-fg-muted focus:outline-none"
                        />
                        <select
                          value={metaForm.status ?? "active"}
                          onChange={(e) => setMetaForm({ ...metaForm, status: e.target.value })}
                          className="rounded border border-line bg-surface-elevated px-2 py-1.5 text-xs text-fg focus:outline-none"
                        >
                          <option value="active">active</option>
                          <option value="paused">paused</option>
                          <option value="winding_down">winding down</option>
                          <option value="completed">completed</option>
                        </select>
                        <label className="text-[11px] text-fg-muted flex items-center gap-2">
                          Renewal
                          <input
                            type="date"
                            value={metaForm.renewal_date ?? ""}
                            onChange={(e) =>
                              setMetaForm({ ...metaForm, renewal_date: e.target.value })
                            }
                            className="flex-1 rounded border border-line bg-surface-elevated px-2 py-1.5 text-xs text-fg focus:outline-none"
                          />
                        </label>
                        <input
                          value={metaForm.retainer ?? ""}
                          onChange={(e) => setMetaForm({ ...metaForm, retainer: e.target.value })}
                          placeholder="Retainer (display only)"
                          className="rounded border border-line bg-surface-elevated px-2 py-1.5 text-xs text-fg placeholder:text-fg-muted focus:outline-none"
                        />
                        <input
                          value={metaForm.primary_contact ?? ""}
                          onChange={(e) =>
                            setMetaForm({ ...metaForm, primary_contact: e.target.value })
                          }
                          placeholder="Primary contact"
                          className="rounded border border-line bg-surface-elevated px-2 py-1.5 text-xs text-fg placeholder:text-fg-muted focus:outline-none"
                        />
                        <input
                          value={metaForm.notes ?? ""}
                          onChange={(e) => setMetaForm({ ...metaForm, notes: e.target.value })}
                          placeholder="Notes"
                          className="rounded border border-line bg-surface-elevated px-2 py-1.5 text-xs text-fg placeholder:text-fg-muted focus:outline-none"
                        />
                        <div className="sm:col-span-2 flex gap-2">
                          <button
                            onClick={() => void handleSaveMeta()}
                            disabled={savingMeta}
                            className="rounded-lg border border-line bg-surface-overlay px-3 py-1.5 text-xs font-medium text-fg hover:border-line-strong disabled:opacity-50"
                          >
                            {savingMeta ? "Saving…" : "Save details"}
                          </button>
                          <button
                            onClick={() => setEditingSlug(null)}
                            disabled={savingMeta}
                            className="rounded-lg border border-line px-3 py-1.5 text-xs text-fg-muted hover:border-line-strong"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button
                        onClick={() => openMetaEditor(c.slug)}
                        className="text-[11px] text-indigo-400 hover:text-indigo-300"
                      >
                        Engagement details
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <p className="mt-8 text-xs text-fg-muted leading-relaxed">
          Only the active client is live — its scheduled actions fire and its
          documents are indexed; parked clients sleep in their slots. Your
          original company is preserved automatically the first time you
          switch, and can be restored from the Company Simulator page.
        </p>
      </div>
    </main>
  );
}
