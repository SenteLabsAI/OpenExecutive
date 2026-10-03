"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  deleteDocument,
  getDocument,
  getSyncedDocument,
  listDocuments,
  listSyncSources,
  listSyncedDocuments,
  syncSourceNow,
  uploadDocument,
  type SyncSourceStatus,
  type SyncedDoc,
  type SyncedSourceId,
  type CompanyDoc,
} from "@/lib/api";
import {
  SOURCE_LABELS,
  filterDocs,
  formatInterval,
  formatSize,
  mergeDocs,
  type DocRow,
  type DocSource,
} from "@/lib/companyDocs";
import { formatRelativeTime } from "@/lib/relativeTime";
import Icon from "@/components/Icon";
import Button, { buttonClass } from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import SidePanel from "@/components/ui/SidePanel";

interface CompanyPanelProps {
  /** Reports how many documents are listed, for the tab's count. */
  onCountChange?: (count: number) => void;
}

interface Viewing {
  source: DocSource;
  name: string;
  url: string | null;
  content: string;
}

const ACCEPT = ".pdf,.docx,.doc,.xlsx,.xlsm,.csv,.md,.txt";
const POLL_MS = 3000;

const PROSE_CLASS =
  "prose prose-sm sm:prose-base max-w-none prose-p:text-fg prose-headings:text-fg prose-strong:text-fg prose-code:text-accent prose-code:bg-surface-overlay prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none prose-pre:bg-surface-overlay prose-pre:border prose-pre:border-line-strong prose-blockquote:border-line-strong prose-blockquote:text-fg-muted prose-ul:text-fg prose-ol:text-fg prose-li:marker:text-fg-muted prose-hr:border-line-strong prose-a:text-accent prose-a:no-underline hover:prose-a:underline prose-table:text-fg prose-th:text-fg prose-th:border-line-strong prose-td:border-line-strong";

// The dot beside a document's source name.
const SOURCE_DOT: Record<DocSource, string> = {
  upload: "bg-fg-subtle",
  drive: "bg-emerald-500",
  onedrive: "bg-blue-500",
  notion: "bg-sky-500",
};

export default function CompanyPanel({ onCountChange }: CompanyPanelProps) {
  const [uploads, setUploads] = useState<CompanyDoc[]>([]);
  const [synced, setSynced] = useState<Record<SyncedSourceId, SyncedDoc[]>>({
    drive: [],
    onedrive: [],
    notion: [],
  });
  const [sources, setSources] = useState<SyncSourceStatus[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<DocSource | "all">("all");
  const [query, setQuery] = useState("");
  const [viewing, setViewing] = useState<Viewing | null>(null);
  const [viewLoading, setViewLoading] = useState<string | null>(null);
  const [syncMessage, setSyncMessage] = useState<Partial<Record<SyncedSourceId, string>>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadSynced = useCallback(async (ids: SyncedSourceId[]) => {
    const lists = await Promise.all(
      ids.map((id) => listSyncedDocuments(id).then((l) => [id, l.files] as const))
    );
    setSynced((prev) => {
      const next = { ...prev };
      for (const [id, files] of lists) next[id] = files;
      return next;
    });
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [docs, srcs] = await Promise.all([listDocuments(), listSyncSources()]);
      setUploads(docs);
      setSources(srcs);
      // A source that was connected once keeps its files listed until the
      // next sync purges them, so list any source with a run on record.
      await loadSynced(srcs.filter((s) => s.enabled || s.last_run).map((s) => s.id));
    } catch {
      setError("Failed to load documents");
    } finally {
      setLoaded(true);
    }
  }, [loadSynced]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const rows = useMemo(
    () => mergeDocs(uploads, synced.drive, synced.notion, synced.onedrive),
    [uploads, synced]
  );
  const shown = useMemo(() => filterDocs(rows, sourceFilter, query), [rows, sourceFilter, query]);

  useEffect(() => {
    if (loaded) onCountChange?.(rows.length);
  }, [loaded, rows.length, onCountChange]);

  // While any source is syncing, poll its status; refresh its files when it
  // finishes.
  const syncingIds = sources.filter((s) => s.syncing).map((s) => s.id);
  const syncingKey = syncingIds.join(",");
  useEffect(() => {
    if (!syncingKey) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await listSyncSources();
        setSources(next);
        const finished = syncingKey
          .split(",")
          .filter((id) => !next.find((s) => s.id === id)?.syncing) as SyncedSourceId[];
        if (finished.length) await loadSynced(finished);
      } catch {
        // keep polling; a transient error shouldn't stop the spinner forever
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [syncingKey, loadSynced]);

  async function handleFiles(files: File[]) {
    if (!files.length) return;
    setError(null);
    const failed: string[] = [];
    for (const [i, file] of files.entries()) {
      setUploadProgress(
        files.length > 1 ? `Adding ${i + 1} of ${files.length}: ${file.name}…` : `Adding ${file.name}…`
      );
      try {
        await uploadDocument(file);
      } catch (e) {
        failed.push(e instanceof Error ? e.message : `Failed to upload ${file.name}`);
      }
    }
    setUploadProgress(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (failed.length) setError(failed.join(" · "));
    try {
      setUploads(await listDocuments());
    } catch {
      setError("Failed to refresh documents");
    }
  }

  async function handleDelete(row: DocRow) {
    if (!confirm(`Delete "${row.name}"? The Executive will stop using it.`)) return;
    setError(null);
    try {
      await deleteDocument(row.ref);
      setUploads((prev) => prev.filter((d) => d.filename !== row.ref));
    } catch {
      setError("Failed to delete document");
    }
  }

  async function handleView(row: DocRow) {
    setError(null);
    setViewLoading(row.key);
    try {
      if (row.source === "upload") {
        const doc = await getDocument(row.ref);
        setViewing({ source: "upload", name: doc.filename, url: null, content: doc.content });
      } else {
        const doc = await getSyncedDocument(row.source, row.ref);
        setViewing({ source: row.source, name: doc.name, url: doc.url, content: doc.content });
      }
    } catch {
      setError("Failed to load document");
    } finally {
      setViewLoading(null);
    }
  }

  async function handleSyncNow(id: SyncedSourceId) {
    setSyncMessage((m) => ({ ...m, [id]: undefined }));
    const problem = await syncSourceNow(id);
    if (problem) {
      setSyncMessage((m) => ({ ...m, [id]: problem }));
      return;
    }
    setSources((prev) => prev.map((s) => (s.id === id ? { ...s, syncing: true } : s)));
  }

  const connected = sources.filter((s) => s.enabled);
  const usedSources = new Set(rows.map((r) => r.source));
  const filterOptions: (DocSource | "all")[] = [
    "all",
    ...(["upload", "drive", "onedrive", "notion"] as DocSource[]).filter((s) => usedSources.has(s)),
  ];

  return (
    <div className="space-y-6 max-w-4xl">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <p className="text-[15px] text-fg-muted max-w-xl">
          Your own files, plus anything synced from a connected source.
        </p>
        <label
          className={buttonClass(
            "primary",
            "md",
            "self-start sm:self-auto flex-shrink-0 cursor-pointer focus-within:ring-2 focus-within:ring-accent/60"
          )}
        >
          <Icon name="plus" size="w-5 h-5" />
          Add documents
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="sr-only"
            accept={ACCEPT}
            onChange={(e) => handleFiles(Array.from(e.target.files ?? []))}
          />
        </label>
      </div>

      {error && (
        <p className="text-sm text-red-500 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3">
          {error}
        </p>
      )}

      <SourcesStrip
        connected={connected}
        anyKnown={sources.length > 0}
        messages={syncMessage}
        onSyncNow={handleSyncNow}
      />

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          handleFiles(Array.from(e.dataTransfer.files));
        }}
        className={`rounded-2xl border-2 border-dashed transition-colors text-center ${
          rows.length === 0 && loaded ? "p-10" : "px-4 py-5"
        } ${dragOver ? "border-accent bg-accent/5" : "border-line-strong/70"}`}
      >
        {uploadProgress ? (
          <p className="text-[15px] text-accent animate-pulse">{uploadProgress}</p>
        ) : (
          <p className="text-[15px] text-fg-muted">
            {rows.length === 0 && loaded
              ? "No documents yet. Drop files here to add your first ones."
              : "Drop files here to add them"}
            <span className="block text-sm text-fg-subtle mt-1">
              PDF, Word, Excel, CSV, Markdown or text, up to 50 MB each
            </span>
          </p>
        )}
      </div>

      {rows.length > 0 && (
        <div className="space-y-3">
          {(filterOptions.length > 2 || rows.length > 8) && (
            <div className="flex items-center gap-2 flex-wrap">
              {filterOptions.length > 2 &&
                filterOptions.map((opt) => (
                  <button
                    key={opt}
                    onClick={() => setSourceFilter(opt)}
                    aria-pressed={sourceFilter === opt}
                    className={`h-10 text-sm font-medium px-4 rounded-xl border transition-colors ${
                      sourceFilter === opt
                        ? "bg-accent/10 text-accent border-accent/30"
                        : "text-fg-muted border-line hover:text-fg hover:bg-surface-overlay"
                    }`}
                  >
                    {opt === "all" ? `All (${rows.length})` : SOURCE_LABELS[opt]}
                  </button>
                ))}
              {rows.length > 8 && (
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search by name…"
                  aria-label="Search documents by name"
                  className="w-full sm:w-60 sm:ml-auto h-10 rounded-xl border border-line bg-surface-elevated px-3 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40"
                />
              )}
            </div>
          )}

          <ul className="divide-y divide-line rounded-2xl border border-line bg-surface-elevated">
            {shown.map((row) => (
              <DocListRow
                key={row.key}
                row={row}
                loading={viewLoading === row.key}
                onView={() => handleView(row)}
                onDelete={() => handleDelete(row)}
              />
            ))}
            {shown.length === 0 && (
              <li className="px-5 py-8 text-[15px] text-fg-subtle text-center">
                No documents match.
              </li>
            )}
          </ul>
        </div>
      )}

      <Viewer doc={viewing} onClose={() => setViewing(null)} />
    </div>
  );
}

function SourcesStrip({
  connected,
  anyKnown,
  messages,
  onSyncNow,
}: {
  connected: SyncSourceStatus[];
  anyKnown: boolean;
  messages: Partial<Record<SyncedSourceId, string>>;
  onSyncNow: (id: SyncedSourceId) => void;
}) {
  if (!anyKnown) return null;
  if (connected.length === 0) {
    return (
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 rounded-2xl border border-line bg-surface-overlay/50 px-5 py-4">
        <p className="flex-1 text-[15px] text-fg-muted">
          Connect Google Drive, OneDrive or Notion to bring documents in automatically.
        </p>
        {/* Sources are connected with environment settings; the guide says how. */}
        <Link
          href="/guide#knowledge"
          className={buttonClass("secondary", "md", "self-start sm:self-auto")}
        >
          How to connect
        </Link>
      </div>
    );
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {connected.map((s) => {
        const dot = s.syncing
          ? "bg-accent animate-pulse"
          : s.last_error
            ? "bg-red-500"
            : s.last_run
              ? "bg-emerald-500"
              : "bg-fg-subtle";
        const message = messages[s.id];
        return (
          <div key={s.id} className="rounded-2xl border border-line bg-surface-elevated px-4 py-3.5">
            <div className="flex items-center gap-2.5">
              <span className={`h-2.5 w-2.5 rounded-full flex-shrink-0 ${dot}`} aria-hidden />
              <span className="text-base font-semibold text-fg truncate">{s.label}</span>
              <Button
                size="sm"
                className="ml-auto !h-10"
                onClick={() => onSyncNow(s.id)}
                disabled={s.syncing}
              >
                {s.syncing ? "Syncing…" : "Sync now"}
              </Button>
            </div>
            <p className="text-sm text-fg-muted mt-1.5">
              {s.syncing
                ? "Checking for new and changed files…"
                : s.last_run
                  ? `Last synced ${formatRelativeTime(s.last_run)} · ${s.file_count} ${
                      s.file_count === 1 ? "file" : "files"
                    }`
                  : "Not synced yet"}
              <span className="text-fg-subtle"> · syncs {formatInterval(s.interval_minutes)}</span>
            </p>
            {(s.last_error || message) && (
              <p className="text-sm text-red-500 mt-1.5">
                {message ?? s.last_error}{" "}
                {!message && (
                  <Link href="/guide#knowledge" className="underline">
                    How to fix
                  </Link>
                )}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function DocListRow({
  row,
  loading,
  onView,
  onDelete,
}: {
  row: DocRow;
  loading: boolean;
  onView: () => void;
  onDelete: () => void;
}) {
  const details: string[] = [];
  if (row.source === "upload") {
    if (row.addedAt) details.push(`Added ${new Date(row.addedAt).toLocaleDateString()}`);
    if (row.sizeBytes !== null) details.push(formatSize(row.sizeBytes));
  } else {
    if (!row.indexed) details.push("No readable text");
    if (row.addedAt) details.push(`Synced ${formatRelativeTime(row.addedAt)}`);
    if (row.editedAt)
      details.push(
        `edited in ${SOURCE_LABELS[row.source]} ${new Date(row.editedAt).toLocaleDateString()}`
      );
  }
  const source = SOURCE_LABELS[row.source];
  const menu: OverflowItem[] = [];
  if (row.url) menu.push({ label: `Open in ${source} ↗`, href: row.url, external: true });
  if (row.source === "upload") {
    menu.push({ label: "Delete", danger: true, onSelect: onDelete });
  } else {
    // Synced files are removed at their source; say so where Delete would be.
    menu.push({ label: `Managed in ${source}: remove it there`, disabled: true });
  }
  return (
    <li className="flex items-center gap-3 sm:gap-4 px-4 sm:px-5 py-3.5">
      <span className="hidden sm:flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-surface-overlay text-fg-muted">
        <Icon name="doc" size="w-5 h-5" />
      </span>
      <div className="min-w-0 flex-1">
        <button
          onClick={onView}
          disabled={!row.indexed}
          className="block max-w-full text-[15px] text-fg font-semibold truncate hover:text-accent disabled:hover:text-fg text-left transition-colors"
        >
          {row.name}
        </button>
        <p className="text-sm text-fg-muted mt-0.5 flex items-center gap-1.5 min-w-0">
          <span className={`h-2 w-2 rounded-full flex-shrink-0 ${SOURCE_DOT[row.source]}`} aria-hidden />
          <span className="flex-shrink-0">{source}</span>
          {row.domain && (
            <span className="hidden sm:inline flex-shrink-0 capitalize text-fg-subtle">· {row.domain}</span>
          )}
          {details.length > 0 && <span className="truncate">· {details.join(" · ")}</span>}
        </p>
      </div>
      <div className="flex items-center gap-1 flex-shrink-0">
        {row.indexed && (
          <Button size="sm" className="!h-10" onClick={onView} disabled={loading}>
            {loading ? "Loading…" : "View"}
          </Button>
        )}
        <OverflowMenu items={menu} label={`More actions for ${row.name}`} />
      </div>
    </li>
  );
}

function Viewer({ doc, onClose }: { doc: Viewing | null; onClose: () => void }) {
  return (
    <SidePanel
      open={doc !== null}
      onClose={onClose}
      width="lg"
      title={<span className="block truncate">{doc?.name}</span>}
      subtitle={
        doc
          ? doc.source === "upload"
            ? "Uploaded document"
            : `From ${SOURCE_LABELS[doc.source]}`
          : undefined
      }
      footer={
        doc?.url ? (
          <a
            href={doc.url}
            target="_blank"
            rel="noopener noreferrer"
            className={buttonClass("secondary", "md")}
          >
            Open original ↗
          </a>
        ) : undefined
      }
    >
      {doc && (
        <div className={PROSE_CLASS}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={SAFE_MARKDOWN}>
            {doc.content}
          </ReactMarkdown>
        </div>
      )}
    </SidePanel>
  );
}

// Synced Drive / OneDrive / Notion text is written by anyone who can edit the shared
// folder or page, so the viewer never fetches its images (a remote image is a
// read beacon) and opens its links in a new tab, away from the app.
const SAFE_MARKDOWN: Components = {
  img: ({ alt }) => <span className="text-fg-subtle">[image{alt ? `: ${alt}` : ""}]</span>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  ),
};
