"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import {
  ArtifactSummary,
  archiveArtifact,
  deleteArtifact,
  listArtifacts,
  restoreArtifact,
  shareArtifact,
  shareLink,
  stopSharingArtifact,
} from "@/lib/api";
import Icon from "@/components/Icon";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import { formatRelativeTime } from "@/lib/relativeTime";
import { PageSpinner } from "@/components/ui/Spinner";

// "c:<name>" is an installed extension's collection (e.g. its own kind of
// document); drafts in a collection show under it, not under Drafts.
type KindFilter = "all" | "draft" | "workflow" | `c:${string}`;

function filterKey(a: ArtifactSummary): KindFilter {
  if (a.collection) return `c:${a.collection}`;
  return a.kind;
}
type View = "active" | "archived";

// How long the "Archived — Undo" toast stays before auto-dismissing.
const UNDO_TIMEOUT_MS = 6000;

// Server group keys (api/routes/artifacts.py `_group`).
const CHATS_GROUP = "chats";
const BRIEFS_GROUP = "briefs";
// A workflow group shows its newest few, then "Show N more".
const GROUP_PREVIEW = 2;
// Wait this long after the last keystroke before searching.
const SEARCH_DELAY_MS = 250;
// How long "Copied" shows on a shared row.
const COPIED_MS = 1500;

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
      aria-pressed={active}
      onClick={onClick}
      className={`min-h-10 flex-shrink-0 rounded-lg px-3.5 text-sm font-medium transition-colors cursor-pointer ${
        active
          ? "bg-accent/10 text-accent"
          : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
      }`}
    >
      {label}
      <span className="ml-1.5 font-normal text-fg-subtle">{count}</span>
    </button>
  );
}

// "Shared by link · Copy · Stop" on a shared row.
function SharedPill({
  item,
  copied,
  pending,
  onCopy,
  onStop,
}: {
  item: ArtifactSummary;
  copied: boolean;
  pending: boolean;
  onCopy: (item: ArtifactSummary) => void;
  onStop: (item: ArtifactSummary) => void;
}) {
  const link = "rounded px-1 font-semibold hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40 cursor-pointer disabled:opacity-50";
  return (
    <span className="hidden sm:inline-flex flex-shrink-0 items-center gap-1 rounded-lg bg-emerald-500/10 px-2.5 py-1 text-sm text-emerald-700 dark:text-emerald-300">
      <span className="font-medium">Shared by link</span>
      <span aria-hidden="true">·</span>
      <button type="button" className={link} onClick={() => onCopy(item)}>
        {copied ? "Copied" : "Copy"}
      </button>
      <span aria-hidden="true">·</span>
      <button
        type="button"
        className={link}
        disabled={pending}
        onClick={() => onStop(item)}
        aria-label={`Stop sharing ${item.title}`}
      >
        Stop
      </button>
    </span>
  );
}

function ArtifactRow({
  item,
  view,
  pending,
  copied,
  onArchive,
  onRestore,
  onDelete,
  onShare,
  onCopy,
  onStop,
}: {
  item: ArtifactSummary;
  view: View;
  pending: boolean;
  copied: boolean;
  onArchive: (item: ArtifactSummary) => void;
  onRestore: (item: ArtifactSummary) => void;
  onDelete: (item: ArtifactSummary) => void;
  onShare: (item: ArtifactSummary) => void;
  onCopy: (item: ArtifactSummary) => void;
  onStop: (item: ArtifactSummary) => void;
}) {
  const shareable = view === "active" && Boolean(item.can_share);
  const shared = shareable && Boolean(item.share_path);
  const shareItems = !shareable
    ? []
    : shared
      ? [
          { label: copied ? "Copied" : "Copy link", onSelect: () => onCopy(item) },
          { label: "Stop sharing", disabled: pending, onSelect: () => onStop(item) },
        ]
      : [{ label: "Share by link", disabled: pending, onSelect: () => onShare(item) }];
  return (
    <div className="group flex items-center gap-2 py-1.5 pl-4 pr-2 hover:bg-surface-overlay/60 transition-colors">
      <Link
        href={`/artifacts/${encodeURIComponent(item.id)}`}
        className="flex min-h-11 min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-0.5 rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
      >
        <span className="min-w-0 truncate text-[15px] font-medium text-fg">{item.title}</span>
        {item.format !== "markdown" && (
          <span className="flex-shrink-0 rounded-full border border-line px-2 py-0.5 text-xs text-fg-muted">
            {item.format_label}
          </span>
        )}
        <span className="ml-auto whitespace-nowrap text-sm text-fg-subtle tabular-nums">
          {formatRelativeTime(item.created_at)}
        </span>
      </Link>

      {shared && (
        <SharedPill item={item} copied={copied} pending={pending} onCopy={onCopy} onStop={onStop} />
      )}
      {shareable && !shared && (
        <Button
          size="sm"
          disabled={pending}
          onClick={() => onShare(item)}
          className="hidden sm:inline-flex flex-shrink-0 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
        >
          Share by link
        </Button>
      )}

      <OverflowMenu
        label={`More for ${item.title}`}
        items={[
          ...shareItems,
          view === "active"
            ? { label: "Archive", disabled: pending, onSelect: () => onArchive(item) }
            : { label: "Restore", disabled: pending, onSelect: () => onRestore(item) },
          {
            label: "Delete permanently",
            danger: true,
            disabled: pending,
            onSelect: () => onDelete(item),
          },
        ]}
      />
    </div>
  );
}

interface UndoToast {
  item: ArtifactSummary;
}

export default function ArtifactsPage() {
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<KindFilter>("all");
  const [view, setView] = useState<View>("active");
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // Workflow groups whose older documents are shown.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [undo, setUndo] = useState<UndoToast | null>(null);
  const undoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // What's typed in the search box, and what the list was last loaded for.
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadSeq = useRef(0);

  const load = useCallback((v: View, q = "") => {
    // Only the newest load lands: a slow answer to an older search never
    // replaces a newer one.
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    listArtifacts({ archived: v === "archived", q })
      .then((items) => {
        if (seq !== loadSeq.current) return;
        setArtifacts(items);
        setSearched(q.trim());
      })
      .catch((e) => {
        if (seq === loadSeq.current) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (seq === loadSeq.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    const t = setTimeout(() => load(view, query), query.trim() ? SEARCH_DELAY_MS : 0);
    return () => clearTimeout(t);
  }, [view, query, load]);

  useEffect(
    () => () => {
      if (undoTimer.current) clearTimeout(undoTimer.current);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
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
        load(view, query); // resync on failure
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, query, setRowPending, showUndo]
  );

  const handleRestore = useCallback(
    async (item: ArtifactSummary) => {
      setRowPending(item.id, true);
      setArtifacts((prev) => prev.filter((a) => a.id !== item.id)); // optimistic
      try {
        await restoreArtifact(item.id);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        load(view, query);
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, query, setRowPending]
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
        load(view, query);
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, query, setRowPending]
  );

  const handleUndo = useCallback(async () => {
    if (!undo) return;
    const { item } = undo;
    dismissUndo();
    try {
      await restoreArtifact(item.id);
      // Resync the current view rather than optimistically guessing — the
      // restored item belongs in Active, and may need to leave Archived.
      load(view, query);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      load(view, query);
    }
  }, [undo, view, query, load, dismissUndo]);

  const copyLink = useCallback(async (id: string, path: string) => {
    try {
      await navigator.clipboard.writeText(shareLink(path));
      setCopiedId(id);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopiedId(null), COPIED_MS);
    } catch {
      // The clipboard can be unavailable (an insecure page); the link stays shared.
    }
  }, []);

  const handleShare = useCallback(
    async (item: ArtifactSummary) => {
      setRowPending(item.id, true);
      try {
        const path = await shareArtifact(item.id);
        setArtifacts((prev) =>
          prev.map((a) => (a.id === item.id ? { ...a, share_path: path } : a))
        );
        await copyLink(item.id, path);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setRowPending(item.id, false);
      }
    },
    [setRowPending, copyLink]
  );

  const handleCopy = useCallback(
    (item: ArtifactSummary) => {
      if (item.share_path) void copyLink(item.id, item.share_path);
    },
    [copyLink]
  );

  const handleStop = useCallback(
    async (item: ArtifactSummary) => {
      setRowPending(item.id, true);
      try {
        await stopSharingArtifact(item.id);
        setArtifacts((prev) =>
          prev.map((a) => (a.id === item.id ? { ...a, share_path: null } : a))
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        load(view, query);
      } finally {
        setRowPending(item.id, false);
      }
    },
    [load, view, query, setRowPending]
  );

  const counts = useMemo(() => {
    const c = { all: artifacts.length, draft: 0, workflow: 0 };
    for (const a of artifacts) {
      if (a.collection) continue;
      if (a.kind === "draft") c.draft++;
      else c.workflow++;
    }
    return c;
  }, [artifacts]);

  // One filter per collection present, in first-seen (newest) order.
  const collections = useMemo(() => {
    const map = new Map<string, { label: string; count: number }>();
    for (const a of artifacts) {
      if (!a.collection) continue;
      const entry = map.get(a.collection);
      if (entry) entry.count++;
      else map.set(a.collection, { label: a.collection_label || a.collection, count: 1 });
    }
    return Array.from(map, ([name, entry]) => ({ name, ...entry }));
  }, [artifacts]);

  // A collection filter whose last item went away shows All.
  const shown: KindFilter =
    filter.startsWith("c:") && !collections.some((c) => `c:${c.name}` === filter)
      ? "all"
      : filter;

  const visible = useMemo(
    () => (shown === "all" ? artifacts : artifacts.filter((a) => filterKey(a) === shown)),
    [artifacts, shown]
  );

  const toggleCollapsed = useCallback(
    (key: string) =>
      setCollapsed((c) => ({ ...c, [key]: !(c[key] ?? key === BRIEFS_GROUP) })),
    []
  );

  const showAll = useCallback(
    (key: string) => setExpanded((prev) => new Set(prev).add(key)),
    []
  );

  // Groups come from the server (`group` / `group_label`). `visible` arrives
  // newest-first, so first-seen order puts the most recent group first;
  // documents from chats lead, and the daily briefs sink to the bottom.
  const groups = useMemo(() => {
    const map = new Map<
      string,
      { key: string; label: string; items: ArtifactSummary[] }
    >();
    for (const a of visible) {
      const existing = map.get(a.group);
      if (existing) existing.items.push(a);
      else map.set(a.group, { key: a.group, label: a.group_label, items: [a] });
    }
    const rank = (key: string) =>
      key === CHATS_GROUP ? 0 : key === BRIEFS_GROUP ? 2 : 1;
    return Array.from(map.values()).sort((x, y) => rank(x.key) - rank(y.key));
  }, [visible]);

  const switchView = useCallback(
    (v: View) => {
      setView(v);
      dismissUndo();
    },
    [dismissUndo]
  );

  const searching = searched !== "";
  const emptyMessage = searching
    ? `Nothing matches “${searched}”.`
    : view === "archived"
      ? "Nothing archived. Documents you archive will collect here, ready to restore."
      : "No documents yet. Reports and memos the Executive produces will collect here.";

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-4 sm:px-6 py-8">
        <div className="max-w-5xl mx-auto">
          <div className="mb-6 min-w-0 max-w-2xl">
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg mb-2">
              {view === "archived" ? "Archived documents" : "Your documents"}
            </h1>
            <p className="text-[15px] text-fg-muted">
              What the Executive made for you. Archive what you&apos;re done with.
            </p>
          </div>
          <div className="mb-6 flex flex-wrap items-center gap-2">
            <label className="relative w-full sm:w-80">
              <span className="sr-only">Search documents</span>
              <Icon
                name="search"
                size="w-4 h-4"
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-fg-subtle"
              />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search titles and what is inside"
                maxLength={200}
                className="h-12 w-full rounded-xl border border-line bg-surface-elevated pl-9 pr-3 text-[15px] text-fg placeholder:text-fg-subtle focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              />
            </label>
            {/* One filter control: the kind, with Active / Archived in its ⋯. */}
            <div className="flex min-w-0 max-w-full items-center gap-1.5">
              <div
                role="group"
                aria-label="Show"
                className="inline-flex max-w-full overflow-x-auto rounded-xl border border-line bg-surface-elevated p-1"
              >
                <FilterButton
                  active={shown === "all"}
                  onClick={() => setFilter("all")}
                  label="All"
                  count={counts.all}
                />
                <FilterButton
                  active={shown === "draft"}
                  onClick={() => setFilter("draft")}
                  label="From chats"
                  count={counts.draft}
                />
                <FilterButton
                  active={shown === "workflow"}
                  onClick={() => setFilter("workflow")}
                  label="Workflows"
                  count={counts.workflow}
                />
                {collections.map((c) => (
                  <FilterButton
                    key={c.name}
                    active={shown === `c:${c.name}`}
                    onClick={() => setFilter(`c:${c.name}`)}
                    label={c.label}
                    count={c.count}
                  />
                ))}
              </div>
              <OverflowMenu
                label="More filters"
                items={[
                  view === "active"
                    ? { label: "Show archived", onSelect: () => switchView("archived") }
                    : { label: "Show active documents", onSelect: () => switchView("active") },
                ]}
              />
            </div>
          </div>

          {view === "archived" && (
            <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface-elevated px-4 py-2.5 text-[15px] text-fg-muted">
              <span>Showing archived documents. Restore one from its ⋯.</span>
              <Button onClick={() => switchView("active")}>
                Back to active
              </Button>
            </div>
          )}

          {loading && (
            <PageSpinner label="Loading documents" />
          )}
          {error && <div className="text-[15px] text-red-400 mb-4">Error: {error}</div>}

          {!loading && !error && artifacts.length === 0 && (
            <div className="rounded-2xl border border-dashed border-line px-5 py-8 text-center text-[15px] text-fg-muted">
              {emptyMessage}
            </div>
          )}

          {!loading && !error && artifacts.length > 0 && (
            <>
              <div className="space-y-6">
                {groups.map((group) => {
                  // While searching, every match shows: no group is closed or cut short.
                  const isCollapsed = searching
                    ? false
                    : collapsed[group.key] ?? group.key === BRIEFS_GROUP;
                  // A closed briefs group still shows the newest one.
                  const capped =
                    !searching && group.key.startsWith("wf:") && !expanded.has(group.key);
                  const rows = isCollapsed
                    ? group.key === BRIEFS_GROUP
                      ? group.items.slice(0, 1)
                      : []
                    : capped
                      ? group.items.slice(0, GROUP_PREVIEW)
                      : group.items;
                  const hidden = isCollapsed ? 0 : group.items.length - rows.length;
                  return (
                    <div key={group.key}>
                      <button
                        type="button"
                        aria-expanded={!isCollapsed}
                        onClick={() => toggleCollapsed(group.key)}
                        className="w-full min-h-10 flex items-center gap-2 mb-2 text-left"
                      >
                        <span
                          aria-hidden="true"
                          className={`text-fg-muted text-xs transition-transform ${
                            isCollapsed ? "" : "rotate-90"
                          }`}
                        >
                          ▶
                        </span>
                        <span className="text-base font-semibold text-fg">
                          {group.label}
                        </span>
                        <span className="text-sm text-fg-muted">
                          {group.items.length}{" "}
                          {group.items.length === 1 ? "document" : "documents"}
                        </span>
                      </button>
                      {rows.length > 0 && (
                        <div className="overflow-hidden rounded-2xl border border-line bg-surface-elevated divide-y divide-line">
                          {rows.map((a) => (
                            <ArtifactRow
                              key={a.id}
                              item={a}
                              view={view}
                              pending={pending.has(a.id)}
                              copied={copiedId === a.id}
                              onArchive={handleArchive}
                              onRestore={handleRestore}
                              onDelete={handleDelete}
                              onShare={handleShare}
                              onCopy={handleCopy}
                              onStop={handleStop}
                            />
                          ))}
                          {hidden > 0 && (
                            <button
                              type="button"
                              onClick={() => showAll(group.key)}
                              className="flex min-h-11 w-full items-center px-4 text-left text-[15px] font-medium text-accent hover:bg-surface-overlay/60 cursor-pointer"
                            >
                              Show {hidden} more
                            </button>
                          )}
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
        <div className="fixed bottom-4 left-4 right-4 sm:left-auto z-50 flex justify-end motion-safe:animate-in motion-safe:slide-in-from-right">
          <div className="flex items-center gap-3 rounded-xl border border-line-strong bg-surface-overlay backdrop-blur shadow-xl shadow-black/40 px-4 py-3">
            <span className="min-w-0 truncate text-[15px] text-fg">
              Archived <span className="font-medium">{undo.item.title}</span>
            </span>
            <Button variant="primary" onClick={handleUndo}>
              Undo
            </Button>
            <button
              type="button"
              onClick={dismissUndo}
              aria-label="Dismiss"
              className="h-9 w-9 -mr-1 flex flex-shrink-0 items-center justify-center rounded-lg text-fg-muted hover:text-fg hover:bg-surface-hover cursor-pointer"
            >
              <Icon name="close" size="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
