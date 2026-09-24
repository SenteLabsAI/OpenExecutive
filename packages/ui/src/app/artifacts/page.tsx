"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import {
  ArtifactSummary,
  WorkflowMeta,
  archiveArtifact,
  deleteArtifact,
  listArtifacts,
  listWorkflows,
  restoreArtifact,
} from "@/lib/api";
import Icon from "@/components/Icon";
import { formatRelativeTime } from "@/lib/relativeTime";

type KindFilter = "all" | "draft" | "workflow";
type View = "active" | "archived";

// How long the "Archived — Undo" toast stays before auto-dismissing.
const UNDO_TIMEOUT_MS = 6000;

function FilterButton({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 text-xs rounded-md ring-1 transition cursor-pointer ${
        active
          ? "bg-surface-elevated ring-indigo-500/40 text-fg"
          : "ring-line text-fg-muted hover:text-fg hover:ring-line-strong"
      }`}
    >
      {label}
      <span className="ml-1.5 text-fg-muted">{count}</span>
    </button>
  );
}

/** A single small icon action on a row. Reveals on hover/focus on desktop,
 *  always visible on touch (no hover) so the controls are never gesture-only. */
function RowAction({
  icon,
  label,
  onClick,
  disabled,
  danger,
}: {
  icon: "archive" | "restore" | "trash";
  label: string;
  onClick: () => void;
  disabled: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={`min-h-8 min-w-8 flex items-center justify-center rounded-md transition cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed ${
        danger
          ? "text-fg-muted hover:text-red-400 hover:bg-red-500/10"
          : "text-fg-muted hover:text-fg hover:bg-surface-elevated"
      }`}
    >
      <Icon name={icon} size="w-4 h-4" />
    </button>
  );
}

function ArtifactRow({
  item,
  view,
  pending,
  onArchive,
  onRestore,
  onDelete,
}: {
  item: ArtifactSummary;
  view: View;
  pending: boolean;
  onArchive: (item: ArtifactSummary) => void;
  onRestore: (item: ArtifactSummary) => void;
  onDelete: (item: ArtifactSummary) => void;
}) {
  return (
    <div className="group flex items-center gap-3 rounded-md px-3 py-2.5 hover:bg-surface-elevated/50 transition">
      <Link
        href={`/artifacts/${encodeURIComponent(item.id)}`}
        className="flex items-center gap-3 min-w-0 flex-1 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500/40"
      >
        <span className="text-sm font-medium text-fg truncate">{item.title}</span>
        {item.format !== "markdown" && (
          <span className="flex-shrink-0 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border border-line text-fg-muted">
            {item.format_label}
          </span>
        )}
      </Link>

      <span className="hidden sm:block text-xs text-fg-muted whitespace-nowrap tabular-nums">
        {formatRelativeTime(item.created_at)}
      </span>

      <div className="flex items-center gap-0.5 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 transition-opacity">
        {view === "active" ? (
          <RowAction
            icon="archive"
            label="Archive"
            disabled={pending}
            onClick={() => onArchive(item)}
          />
        ) : (
          <RowAction
            icon="restore"
            label="Restore"
            disabled={pending}
            onClick={() => onRestore(item)}
          />
        )}
        <RowAction
          icon="trash"
          label="Delete permanently"
          danger
          disabled={pending}
          onClick={() => onDelete(item)}
        />
      </div>
    </div>
  );
}

interface UndoToast {
  item: ArtifactSummary;
}

export default function ArtifactsPage() {
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<KindFilter>("all");
  const [view, setView] = useState<View>("active");
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [undo, setUndo] = useState<UndoToast | null>(null);
  const undoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback((v: View) => {
    setLoading(true);
    setError(null);
    listArtifacts({ archived: v === "archived" })
      .then(setArtifacts)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load(view);
  }, [view, load]);

  // Workflow metadata is view-independent — fetch once to map a run's raw
  // workflow_name (carried as source_label) to its pretty title for group
  // headers. Failure is non-fatal; we fall back to the raw source_label.
  useEffect(() => {
    listWorkflows()
      .then(setWorkflows)
      .catch(() => {});
  }, []);

  useEffect(
    () => () => {
      if (undoTimer.current) clearTimeout(undoTimer.current);
    },
    []
  );

  const setRowPending = useCallback((id: string, on: boolean) => {
    setPending((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const dismissUndo = useCallback(() => {
    if (undoTimer.current) clearTimeout(undoTimer.current);
    setUndo(null);
  }, []);

  const showUndo = useCallback((item: ArtifactSummary) => {
    if (undoTimer.current) clearTimeout(undoTimer.current);
    setUndo({ item });
    undoTimer.current = setTimeout(() => setUndo(null), UNDO_TIMEOUT_MS);
  }, []);

  const handleArchive = useCallback(
    async (item: ArtifactSummary) => {
      setRowPending(item.id, true);
      setArtifacts((prev) => prev.filter((a) => a.id !== item.id)); // optimistic
      try {
        await archiveArtifact(item.id);
        showUndo(item);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        load(view); // resync on failure
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, setRowPending, showUndo]
  );

  const handleRestore = useCallback(
    async (item: ArtifactSummary) => {
      setRowPending(item.id, true);
      setArtifacts((prev) => prev.filter((a) => a.id !== item.id)); // optimistic
      try {
        await restoreArtifact(item.id);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        load(view);
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, setRowPending]
  );

  const handleDelete = useCallback(
    async (item: ArtifactSummary) => {
      if (
        !confirm(
          `Permanently delete "${item.title}"? This removes it everywhere and cannot be undone.`
        )
      )
        return;
      setRowPending(item.id, true);
      setArtifacts((prev) => prev.filter((a) => a.id !== item.id)); // optimistic
      try {
        await deleteArtifact(item.id);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        load(view);
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, setRowPending]
  );

  const handleUndo = useCallback(async () => {
    if (!undo) return;
    const { item } = undo;
    dismissUndo();
    try {
      await restoreArtifact(item.id);
      // Resync the current view rather than optimistically guessing — the
      // restored item belongs in Active, and may need to leave Archived.
      load(view);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      load(view);
    }
  }, [undo, view, load, dismissUndo]);

  const counts = useMemo(() => {
    const c = { all: artifacts.length, draft: 0, workflow: 0 };
    for (const a of artifacts) {
      if (a.kind === "draft") c.draft++;
      else c.workflow++;
    }
    return c;
  }, [artifacts]);

  const visible = useMemo(
    () => (filter === "all" ? artifacts : artifacts.filter((a) => a.kind === filter)),
    [artifacts, filter]
  );

  const workflowTitleMap = useMemo(
    () => new Map(workflows.map((w) => [w.name, w.title] as const)),
    [workflows]
  );

  const toggleCollapsed = useCallback(
    (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] })),
    []
  );

  // Group by source, mirroring the Jobs → Runs view: each workflow becomes its
  // own group (keyed by source_label = workflow_name, titled via workflowTitleMap),
  // and every draft collapses into one "Drafts" group. `visible` arrives
  // newest-first from the API, so first-seen order surfaces the group with the
  // most recent artifact first.
  const groups = useMemo(() => {
    const map = new Map<
      string,
      { key: string; label: string; items: ArtifactSummary[] }
    >();
    for (const a of visible) {
      // Namespace the workflow key so a workflow literally named "drafts"
      // can never collide with the drafts bucket.
      const key = a.kind === "draft" ? "drafts" : `wf:${a.source_label}`;
      const existing = map.get(key);
      if (existing) {
        existing.items.push(a);
      } else {
        const label =
          a.kind === "draft"
            ? "Drafts"
            : workflowTitleMap.get(a.source_label) ?? a.source_label;
        map.set(key, { key, label, items: [a] });
      }
    }
    return Array.from(map.values());
  }, [visible, workflowTitleMap]);

  const emptyMessage =
    view === "archived"
      ? "Nothing archived. Documents you archive will collect here, ready to restore."
      : "No documents yet. Reports and memos the Executive produces will collect here.";

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-6 py-8">
        <div className="max-w-5xl mx-auto">
          <div className="mb-6">
            <h1 className="text-2xl font-semibold text-fg mb-1">
              Executive Documents
            </h1>
            <p className="text-sm text-fg-muted">
              Every deliverable the Executive has produced — memos, web
              pages, Word documents, spreadsheets and links into your connected
              apps, alongside completed workflow outputs.
              Archive what you&apos;re done with; delete clears it for good.
            </p>
          </div>

          {/* Active / Archived view toggle */}
          <div className="inline-flex items-center gap-1 mb-4 p-0.5 rounded-lg ring-1 ring-line bg-surface/40">
            {(["active", "archived"] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => {
                  setView(v);
                  dismissUndo();
                }}
                className={`px-3 py-1.5 text-xs rounded-md transition cursor-pointer capitalize ${
                  view === v
                    ? "bg-surface-elevated text-fg"
                    : "text-fg-muted hover:text-fg"
                }`}
              >
                {v}
              </button>
            ))}
          </div>

          {loading && (
            <div className="text-sm text-fg-muted">Loading artifacts…</div>
          )}
          {error && <div className="text-sm text-red-400 mb-4">Error: {error}</div>}

          {!loading && !error && artifacts.length === 0 && (
            <div className="text-sm text-fg-muted">{emptyMessage}</div>
          )}

          {!loading && !error && artifacts.length > 0 && (
            <>
              <div className="flex items-center gap-1 mb-5">
                <FilterButton
                  active={filter === "all"}
                  onClick={() => setFilter("all")}
                  label="All"
                  count={counts.all}
                />
                <FilterButton
                  active={filter === "draft"}
                  onClick={() => setFilter("draft")}
                  label="Drafts"
                  count={counts.draft}
                />
                <FilterButton
                  active={filter === "workflow"}
                  onClick={() => setFilter("workflow")}
                  label="Workflows"
                  count={counts.workflow}
                />
              </div>

              <div className="space-y-5">
                {groups.map((group) => {
                  const isCollapsed = !!collapsed[group.key];
                  return (
                    <div key={group.key}>
                      <button
                        type="button"
                        onClick={() => toggleCollapsed(group.key)}
                        className="w-full flex items-center gap-2 mb-2 text-left"
                      >
                        <span
                          className={`text-fg-muted text-xs transition-transform ${
                            isCollapsed ? "" : "rotate-90"
                          }`}
                        >
                          ▶
                        </span>
                        <span className="text-sm font-semibold text-fg">
                          {group.label}
                        </span>
                        <span className="text-xs text-fg-muted">
                          {group.items.length}{" "}
                          {group.items.length === 1 ? "artifact" : "artifacts"}
                        </span>
                      </button>
                      {!isCollapsed && (
                        <div className="rounded-lg border border-line bg-surface/40 divide-y divide-line/60">
                          {group.items.map((a) => (
                            <ArtifactRow
                              key={a.id}
                              item={a}
                              view={view}
                              pending={pending.has(a.id)}
                              onArchive={handleArchive}
                              onRestore={handleRestore}
                              onDelete={handleDelete}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </main>

      {/* Undo toast after archive */}
      {undo && (
        <div className="fixed bottom-4 right-4 z-50 motion-safe:animate-in motion-safe:slide-in-from-right">
          <div className="flex items-center gap-3 rounded-xl border border-line-strong bg-surface-overlay backdrop-blur shadow-xl shadow-black/40 px-4 py-3">
            <span className="text-sm text-fg">
              Archived <span className="font-medium">{undo.item.title}</span>
            </span>
            <button
              type="button"
              onClick={handleUndo}
              className="text-sm font-medium text-indigo-400 hover:text-indigo-300 cursor-pointer"
            >
              Undo
            </button>
            <button
              type="button"
              onClick={dismissUndo}
              aria-label="Dismiss"
              className="min-h-8 min-w-8 -mr-1 flex items-center justify-center rounded text-fg-muted hover:text-fg cursor-pointer"
            >
              <Icon name="close" size="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
