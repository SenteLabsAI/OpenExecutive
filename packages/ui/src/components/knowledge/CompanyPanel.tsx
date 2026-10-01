"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
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

interface CompanyPanelProps {
  /** Reports how many documents are listed, for the sidebar count. */
  onCountChange?: (count: number) => void;
}

interface Viewing {
  source: DocSource;
  name: string;
  url: string | null;
  content: string;
}

const ACCEPT = ".pdf,.docx,.doc,.md,.txt";
const POLL_MS = 3000;

const PROSE_CLASS =
  "prose prose-invert prose-sm max-w-none prose-p:text-fg prose-headings:text-fg prose-strong:text-fg prose-code:text-indigo-300 prose-code:bg-surface-overlay prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none prose-pre:bg-surface-overlay prose-pre:border prose-pre:border-line-strong prose-blockquote:border-line-strong prose-blockquote:text-fg-muted prose-ul:text-fg prose-ol:text-fg prose-li:marker:text-fg-muted prose-hr:border-line-strong prose-a:text-indigo-400 prose-a:no-underline hover:prose-a:underline prose-table:text-fg prose-th:text-fg prose-th:border-line-strong prose-td:border-line-strong";

const BADGE_CLASS: Record<DocSource, string> = {
  upload: "bg-surface-input text-fg-muted border-line-strong",
  drive: "bg-emerald-500/15 text-emerald-400 border-emerald-500/40",
  notion: "bg-sky-500/15 text-sky-400 border-sky-500/40",
};

export default function CompanyPanel({ onCountChange }: CompanyPanelProps) {
  const [uploads, setUploads] = useState<CompanyDoc[]>([]);
  const [synced, setSynced] = useState<Record<SyncedSourceId, SyncedDoc[]>>({
    drive: [],
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
    () => mergeDocs(uploads, synced.drive, synced.notion),
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

  // Close the viewer on Escape.
  useEffect(() => {
    if (!viewing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setViewing(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [viewing]);

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
    ...(["upload", "drive", "notion"] as DocSource[]).filter((s) => usedSources.has(s)),
  ];

  return (
    <div className="space-y-5 max-w-3xl">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-fg">Company documents</h1>
          <p className="text-sm text-fg-muted mt-1">
            Everything here is read by the Executive when it answers questions about your
            company.
          </p>
        </div>
        <label className="flex-shrink-0 cursor-pointer inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-sm font-medium transition-colors">
          <Icon name="plus" size="w-4 h-4" />
          Add documents
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            accept={ACCEPT}
            onChange={(e) => handleFiles(Array.from(e.target.files ?? []))}
          />
        </label>
      </div>

      {error && (
        <p className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
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
        className={`rounded-xl border-2 border-dashed transition-colors text-center ${
          rows.length === 0 && loaded ? "p-10" : "px-4 py-4"
        } ${dragOver ? "border-indigo-500 bg-indigo-500/5" : "border-line-strong"}`}
      >
        {uploadProgress ? (
          <p className="text-sm text-indigo-400 animate-pulse">{uploadProgress}</p>
        ) : (
          <p className="text-sm text-fg-muted">
            {rows.length === 0 && loaded
              ? "No documents yet. Drop files here to add your first ones."
              : "Drop files here to add them"}
            <span className="block text-xs text-fg-subtle mt-1">
              PDF, Word, Markdown or text, up to 50 MB each
            </span>
          </p>
        )}
      </div>

      {rows.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            {filterOptions.length > 2 &&
              filterOptions.map((opt) => (
                <button
                  key={opt}
                  onClick={() => setSourceFilter(opt)}
                  className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                    sourceFilter === opt
                      ? "bg-surface-input text-fg border-line-strong"
                      : "text-fg-muted border-transparent hover:text-fg"
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
                className="ml-auto w-48 rounded-lg border border-line bg-surface-elevated px-2.5 py-1 text-xs text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/40"
              />
            )}
          </div>

          <ul className="divide-y divide-line-strong/50 rounded-xl border border-line-strong/50 bg-surface-overlay/40">
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
              <li className="px-4 py-6 text-sm text-fg-subtle text-center">
                No documents match.
              </li>
            )}
          </ul>
        </div>
      )}

      {viewing && <Viewer doc={viewing} onClose={() => setViewing(null)} />}
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
      <p className="text-xs text-fg-subtle">
        Connect Google Drive or Notion to bring documents in automatically.{" "}
        <Link href="/settings/integrations" className="text-indigo-400 hover:underline">
          Set up in Settings
        </Link>
      </p>
    );
  }
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {connected.map((s) => {
        const dot = s.syncing
          ? "bg-indigo-400 animate-pulse"
          : s.last_error
            ? "bg-red-400"
            : s.last_run
              ? "bg-emerald-400"
              : "bg-fg-subtle";
        const message = messages[s.id];
        return (
          <div
            key={s.id}
            className="rounded-xl border border-line-strong/60 bg-surface-overlay/40 px-3.5 py-2.5"
          >
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden />
              <span className="text-sm font-medium text-fg">{s.label}</span>
              <button
                onClick={() => onSyncNow(s.id)}
                disabled={s.syncing}
                className="ml-auto text-xs px-2.5 py-1 rounded-lg border border-line-strong text-fg hover:bg-surface-input disabled:opacity-60 disabled:cursor-default transition-colors"
              >
                {s.syncing ? "Syncing…" : "Sync now"}
              </button>
            </div>
            <p className="text-xs text-fg-muted mt-1">
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
              <p className="text-xs text-red-400 mt-1">
                {message ?? s.last_error}{" "}
                {!message && (
                  <Link href="/settings/integrations" className="underline">
                    Check settings
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
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <Icon name="doc" size="w-4 h-4" className="text-fg-subtle flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0">
          <button
            onClick={onView}
            disabled={!row.indexed}
            className="text-sm text-fg font-medium truncate hover:text-indigo-300 disabled:hover:text-fg text-left transition-colors"
          >
            {row.name}
          </button>
          <span
            className={`flex-shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded border ${BADGE_CLASS[row.source]}`}
          >
            {SOURCE_LABELS[row.source]}
          </span>
          {row.domain && (
            <span className="flex-shrink-0 text-[10px] text-fg-subtle capitalize">
              {row.domain}
            </span>
          )}
        </div>
        <p className="text-xs text-fg-muted mt-0.5 truncate">{details.join(" · ")}</p>
      </div>
      <div className="flex items-center gap-1 flex-shrink-0">
        {row.indexed && (
          <button
            onClick={onView}
            disabled={loading}
            className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50 px-2 py-1 rounded transition-colors"
          >
            {loading ? "Loading…" : "View"}
          </button>
        )}
        {row.url && (
          <a
            href={row.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-fg-muted hover:text-fg px-2 py-1 rounded transition-colors"
          >
            Open in {SOURCE_LABELS[row.source]} ↗
          </a>
        )}
        {row.source === "upload" ? (
          <button
            onClick={onDelete}
            className="text-xs text-red-400 hover:text-red-300 px-2 py-1 rounded transition-colors"
          >
            Delete
          </button>
        ) : (
          <span
            className="text-[11px] text-fg-subtle px-2"
            title={`Remove it from ${SOURCE_LABELS[row.source]} and it drops off on the next sync.`}
          >
            Managed in {SOURCE_LABELS[row.source]}
          </span>
        )}
      </div>
    </li>
  );
}

function Viewer({ doc, onClose }: { doc: Viewing; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-2xl border border-line-strong bg-surface-elevated shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b border-line-strong/60 px-6 py-4">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-widest text-indigo-400">
              {doc.source === "upload" ? "Uploaded document" : `From ${SOURCE_LABELS[doc.source]}`}
            </p>
            <h3 className="truncate text-base font-semibold text-fg">{doc.name}</h3>
          </div>
          <div className="flex items-center gap-1 flex-shrink-0">
            {doc.url && (
              <a
                href={doc.url}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-lg px-3 py-1.5 text-xs text-fg-muted hover:bg-surface-overlay hover:text-fg transition-colors"
              >
                Open original ↗
              </a>
            )}
            <button
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-xs text-fg-muted hover:bg-surface-overlay hover:text-fg transition-colors"
            >
              Close
            </button>
          </div>
        </div>
        <div className={`flex-1 overflow-y-auto px-6 py-5 ${PROSE_CLASS}`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{doc.content}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}
