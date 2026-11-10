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
  listUnansweredQuestions,
  markQuestionDocumented,
  listSyncSources,
  listSyncedDocuments,
  syncSourceNow,
  uploadDocument,
  type SyncSourceStatus,
  type SyncedDoc,
  type SyncedSourceId,
  type CompanyDoc,
  type UnansweredQuestion,
} from "@/lib/api";
import {
  SOURCE_LABELS,
  acceptsDrop,
  dragLeavesPanel,
  dropLandsOnPanel,
  filterDocs,
  formatInterval,
  formatSize,
  mergeDocs,
  showEmptyDropBox,
  type DocRow,
  type DocSource,
} from "@/lib/companyDocs";
import { formatRelativeTime } from "@/lib/relativeTime";
import Icon from "@/components/Icon";
import Button, { buttonClass } from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import SidePanel from "@/components/ui/SidePanel";
import { Spinner } from "@/components/ui/Spinner";
import UnansweredQuestions from "./UnansweredQuestions";

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
  // Set before the first await, so two drops in one frame can't both start.
  const uploadingRef = useRef(false);
  // Questions it could not answer, and the one whose document is being
  // added through the file picker (it leaves the list once the upload works).
  const [questions, setQuestions] = useState<UnansweredQuestion[]>([]);
  const [addingFor, setAddingFor] = useState<number | null>(null);
  const addingForRef = useRef<number | null>(null);

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

  useEffect(() => {
    listUnansweredQuestions()
      .then((page) => setQuestions(page.questions))
      .catch(() => {});
  }, []);

  function addDocumentFor(id: number) {
    addingForRef.current = id;
    fileInputRef.current?.click();
  }

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
    // One batch at a time, from a drop or from Add documents.
    if (!acceptsDrop(uploadingRef.current, files.length)) return;
    uploadingRef.current = true;
    setError(null);
    const failed: string[] = [];
    const forQuestion = addingForRef.current;
    addingForRef.current = null;
    setAddingFor(forQuestion);
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
    uploadingRef.current = false;
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (failed.length) setError(failed.join(" · "));
    if (forQuestion !== null && failed.length < files.length) {
      try {
        setQuestions((await markQuestionDocumented(forQuestion)).questions);
      } catch {
        // The document is in; the question stays listed and can be dismissed.
      }
    }
    setAddingFor(null);
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

  const empty = loaded && rows.length === 0;
  const dropHandlers = {
        onDragOver: (e: React.DragEvent) => {
          if (!dropLandsOnPanel(e.currentTarget, e.target)) return;
          if (!e.dataTransfer.types.includes("Files")) return;
          // Always claim a file drag, so the browser never opens the file in
          // place of the page; while a batch is still being added, refuse it.
          e.preventDefault();
          if (uploadingRef.current) {
            e.dataTransfer.dropEffect = "none";
            return;
          }
          setDragOver(true);
        },
        onDragLeave: (e: React.DragEvent) => {
          // Only when the pointer leaves the panel, not when it crosses a child.
          if (dragLeavesPanel(e.currentTarget, e.relatedTarget)) setDragOver(false);
        },
        onDrop: (e: React.DragEvent) => {
          if (!dropLandsOnPanel(e.currentTarget, e.target)) return;
          e.preventDefault();
          setDragOver(false);
          addingForRef.current = null;
          handleFiles(Array.from(e.dataTransfer.files));
        },
      };

  // The side panels sit outside the drop area, so a file dropped on one
  // isn't uploaded.
  return (
    <>
    <UnansweredQuestions
      questions={questions}
      onChange={(page) => setQuestions(page.questions)}
      onAddDocument={addDocumentFor}
      addingFor={addingFor}
    />
    <div
      {...dropHandlers}
      className={`space-y-4 max-w-5xl rounded-2xl transition-shadow ${
        dragOver && !empty ? "ring-2 ring-accent ring-offset-8 ring-offset-surface" : ""
      }`}
    >
      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <h2 className="text-lg font-semibold text-fg flex-1">
          Documents
          {rows.length > 0 && (
            <span className="ml-2 font-medium text-fg-subtle tabular-nums">{rows.length}</span>
          )}
        </h2>
        <div className="flex flex-wrap items-center gap-3">
          <label
              // Adding documents from here is not for a listed question.
              // (A click forwarded to the input bubbles here too: keep that one.)
              onClick={(e) => {
                if (e.target !== fileInputRef.current) addingForRef.current = null;
              }}
              className={buttonClass(
                "primary",
                "md",
                "flex-shrink-0 cursor-pointer focus-within:ring-2 focus-within:ring-accent/60"
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
      </div>

      <SourcesLine
        connected={connected}
        anyKnown={sources.length > 0}
        messages={syncMessage}
        onSyncNow={handleSyncNow}
      />

      {error && (
        <p className="text-sm text-red-500 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3">
          {error}
        </p>
      )}

      {uploadProgress && <p className="text-[15px] text-accent animate-pulse">{uploadProgress}</p>}

      {showEmptyDropBox({ loaded, count: rows.length, uploading: uploadProgress !== null }) && (
        <div
          className={`rounded-2xl border-2 border-dashed transition-colors text-center px-6 py-14 ${
            dragOver ? "border-accent bg-accent/5" : "border-line-strong/70"
          }`}
        >
          <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-surface-overlay text-fg-muted">
            <Icon name="doc" size="w-6 h-6" />
          </span>
          <p className="text-[17px] font-semibold text-fg">Add your first documents</p>
          <p className="text-sm text-fg-muted mt-1">
            Drop files here, or press Add documents. PDF, Word, Excel, CSV, Markdown or text, up to 50 MB
            each.
          </p>
        </div>
      )}

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

          <p className={`text-sm text-center ${dragOver ? "text-accent" : "text-fg-subtle"}`}>
            Drop files anywhere here to add them · PDF, Word, Excel, CSV, Markdown or text, up to 50 MB
            each
          </p>
        </div>
      )}

    </div>
      <Viewer doc={viewing} onClose={() => setViewing(null)} />
    </>
  );
}

// One line under the heading: each connected source with when it last
// synced, or how to connect one.
function SourcesLine({
  connected,
  anyKnown,
  messages,
  onSyncNow,
}: {
  connected: SyncSourceStatus[];
  anyKnown: boolean;
  messages: Partial<Record<SyncedSourceId, string>>;
  onSyncNow?: (id: SyncedSourceId) => void;
}) {
  if (!anyKnown) return null;
  if (connected.length === 0) {
    return (
      <p className="text-sm text-fg-muted">
        Connect Google Drive, OneDrive or Notion to bring documents in automatically.{" "}
        {/* Sources are connected with environment settings; the guide says how. */}
        <Link href="/guide#knowledge" className="font-medium text-accent hover:underline">
          How to connect
        </Link>
      </p>
    );
  }
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-sm text-fg-muted">
        {connected.map((s) => {
          const dot = s.syncing
            ? "bg-accent animate-pulse"
            : s.last_error
              ? "bg-red-500"
              : s.last_run
                ? "bg-emerald-500"
                : "bg-fg-subtle";
          const when = s.syncing
            ? "syncing…"
            : s.last_run
              ? `synced ${formatRelativeTime(s.last_run)} · ${s.file_count} ${s.file_count === 1 ? "file" : "files"}`
              : "not synced yet";
          return (
            <span
              key={s.id}
              className="inline-flex items-center gap-2"
              title={`Syncs ${formatInterval(s.interval_minutes)}`}
            >
              <span className={`h-2 w-2 rounded-full flex-shrink-0 ${dot}`} aria-hidden />
              <span>
                <span className="text-fg">{s.label}</span> · {when}
              </span>
              {onSyncNow && !s.syncing && (
                <button
                  type="button"
                  onClick={() => onSyncNow(s.id)}
                  className="font-medium text-accent hover:underline"
                >
                  Sync now
                </button>
              )}
            </span>
          );
        })}
      </div>
      {connected.map((s) => {
        const message = messages[s.id];
        if (!s.last_error && !message) return null;
        return (
          <p key={s.id} className="text-sm text-red-500">
            {s.label}: {message ?? s.last_error}{" "}
            {!message && (
              <Link href="/guide#knowledge" className="underline">
                How to fix
              </Link>
            )}
          </p>
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
    if (!row.indexed)
      details.push(row.unreadableReason ? `No readable text: ${row.unreadableReason}` : "No readable text");
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
            <span className="inline-flex items-center gap-2">
              {loading && <Spinner />}
              View
            </span>
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
