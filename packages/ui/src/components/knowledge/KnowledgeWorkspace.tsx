"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  createBuiltinFile,
  createFailureFile,
  deleteBuiltinFile,
  deleteFailureFile,
  getBuiltinFile,
  getFailureFile,
  getReviewItem,
  getReviewStats,
  listBuiltinFiles,
  listFailureFiles,
  patchReviewItem,
  updateBuiltinFile,
  updateFailureFile,
  type BuiltinFileContent,
  type BuiltinFileMeta,
  type ReviewItem,
  type ReviewStatus,
} from "@/lib/api";
import ReviewQueue from "@/components/ReviewQueue";
import CompanyPanel from "./CompanyPanel";
import FileEditor from "./FileEditor";
import NewFileForm from "./NewFileForm";
import QueryPanel from "./QueryPanel";
import ReferencePanel from "./ReferencePanel";
import SourceTree, { type FileKind, type Selection } from "./SourceTree";

const DOMAINS = [
  "board",
  "finance",
  "hr",
  "legal",
  "marketing",
  "operations",
  "product",
  "sales",
  "strategy",
];

// Review item ids are `<content_type>:<domain>:<filename>`
// (knowledge/review_store.py build_item_id). Failure docs use `failure`.
function reviewItemId(fileKind: FileKind, domain: string, filename: string): string {
  return `${fileKind === "failures" ? "failure" : "builtin"}:${domain}:${filename}`;
}

export default function KnowledgeWorkspace() {
  const searchParams = useSearchParams();
  const [builtinFiles, setBuiltinFiles] = useState<BuiltinFileMeta[]>([]);
  const [failureFiles, setFailureFiles] = useState<BuiltinFileMeta[]>([]);
  // `/knowledge?view=review` (and the old `/review` route, which redirects
  // here) opens straight onto the review queue.
  const [selection, setSelection] = useState<Selection>(() =>
    searchParams.get("view") === "review" ? { kind: "review" } : null
  );
  const [reviewCount, setReviewCount] = useState(0);
  const [fileReview, setFileReview] = useState<ReviewItem | null>(null);
  // Bumped on every review-status request; only the latest may write, so a
  // slow response (another file, or a refetch racing an Approve) can't
  // overwrite a newer one.
  const reviewSeq = useRef(0);

  // Also follow `?view=review` on in-app navigation, where the page is not
  // remounted and the initializer above doesn't run again.
  const view = searchParams.get("view");
  useEffect(() => {
    if (view === "review") setSelection({ kind: "review" });
  }, [view]);
  const [selectedContent, setSelectedContent] = useState<BuiltinFileContent | null>(null);
  const [editContent, setEditContent] = useState("");
  const [isDirty, setIsDirty] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  const loadIndex = useCallback(async () => {
    try {
      const [b, f] = await Promise.all([listBuiltinFiles(), listFailureFiles()]);
      setBuiltinFiles(b);
      setFailureFiles(f);
    } catch {
      setError("Failed to load knowledge index");
    }
  }, []);

  useEffect(() => {
    loadIndex();
  }, [loadIndex]);

  // Refetched whenever the view changes, so approving in the queue updates
  // the tree's count once you move on.
  useEffect(() => {
    getReviewStats()
      .then((s) => setReviewCount(s.pending + s.needs_revision))
      .catch(() => {});
  }, [selection]);

  const loadFileReview = useCallback(async (sel: Selection) => {
    const seq = ++reviewSeq.current;
    if (sel?.kind !== "file") return;
    try {
      const detail = await getReviewItem(reviewItemId(sel.fileKind, sel.domain, sel.filename));
      if (reviewSeq.current === seq) setFileReview(detail.item);
    } catch {
      // No review record (e.g. a file added outside the app before
      // registration) — show no status rather than an error.
      if (reviewSeq.current === seq) setFileReview(null);
    }
  }, []);

  useEffect(() => {
    setFileReview(null);
    loadFileReview(selection);
  }, [selection, loadFileReview]);

  async function handleSetReviewStatus(status: ReviewStatus) {
    if (!fileReview) return;
    const seq = ++reviewSeq.current;
    try {
      const updated = await patchReviewItem(fileReview.item_id, { status });
      if (reviewSeq.current === seq) setFileReview(updated);
      const stats = await getReviewStats();
      setReviewCount(stats.pending + stats.needs_revision);
    } catch {
      setError("Failed to update review status");
    }
  }

  // Load file content whenever selection points at a file.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setSelectedContent(null);
      setEditContent("");
      setIsDirty(false);
      if (selection?.kind !== "file") return;
      try {
        const fetcher = selection.fileKind === "builtin" ? getBuiltinFile : getFailureFile;
        const data = await fetcher(selection.domain, selection.filename);
        if (cancelled) return;
        setSelectedContent(data);
        setEditContent(data.content);
      } catch {
        if (!cancelled) setError("Failed to load file");
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [selection]);

  async function handleSave() {
    if (selection?.kind !== "file" || !selectedContent) return;
    const updater =
      selection.fileKind === "builtin" ? updateBuiltinFile : updateFailureFile;
    setIsSaving(true);
    setError(null);
    try {
      await updater(selection.domain, selection.filename, editContent);
      setIsDirty(false);
      // The server moves an edited file to needs_revision; reflect that now.
      await loadFileReview(selection);
    } catch {
      setError("Failed to save file");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleDelete() {
    if (selection?.kind !== "file" || !selectedContent) return;
    if (
      !confirm(
        `Delete "${selectedContent.filename}"? This removes it from the knowledge base.`
      )
    )
      return;
    const deleter =
      selection.fileKind === "builtin" ? deleteBuiltinFile : deleteFailureFile;
    try {
      await deleter(selection.domain, selection.filename);
      setSelection(null);
      await loadIndex();
    } catch {
      setError("Failed to delete file");
    }
  }

  async function handleCreate(
    fileKind: FileKind,
    domain: string,
    filename: string,
    content: string
  ) {
    const creator = fileKind === "builtin" ? createBuiltinFile : createFailureFile;
    await creator(domain, filename, content);
    await loadIndex();
    setSelection({ kind: "file", fileKind, domain, filename });
  }

  const openFile = useCallback(
    (fileKind: FileKind, domain: string, filename: string) => {
      setSelection({ kind: "file", fileKind, domain, filename });
    },
    []
  );

  return (
    <div className="flex h-full">
      <aside className="w-64 flex-shrink-0 border-r border-line bg-surface/40 px-4 py-5 overflow-y-auto">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter files…"
          className="w-full mb-4 rounded-lg border border-line bg-surface-elevated px-2.5 py-1.5 text-xs text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/40"
        />
        <SourceTree
          domains={DOMAINS}
          builtinFiles={builtinFiles}
          failureFiles={failureFiles}
          selection={selection}
          filter={filter}
          reviewCount={reviewCount}
          onSelect={setSelection}
        />
      </aside>

      <main className="flex-1 min-w-0 overflow-y-auto px-8 py-6">
        {error && (
          <div className="mb-4 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        {selection === null && (
          <EmptyState />
        )}

        {selection?.kind === "file" && selectedContent && (
          <FileEditor
            file={selectedContent}
            content={editContent}
            isDirty={isDirty}
            isSaving={isSaving}
            variant={selection.fileKind === "failures" ? "failure" : "playbook"}
            review={fileReview}
            onSetReviewStatus={handleSetReviewStatus}
            onChange={(v) => {
              setEditContent(v);
              setIsDirty(true);
            }}
            onSave={handleSave}
            onDelete={handleDelete}
          />
        )}

        {selection?.kind === "file" && !selectedContent && !error && (
          <p className="text-sm text-fg-muted">Loading…</p>
        )}

        {selection?.kind === "new" && (
          <NewFileForm
            domains={DOMAINS}
            initialDomain={DOMAINS[0]}
            variant={selection.fileKind === "failures" ? "failure" : "playbook"}
            onSave={(domain, filename, content) =>
              handleCreate(selection.fileKind, domain, filename, content)
            }
            onCancel={() => setSelection(null)}
          />
        )}

        {selection?.kind === "review" && <ReviewQueue />}
        {selection?.kind === "company" && <CompanyPanel domains={DOMAINS} />}
        {selection?.kind === "reference" && <ReferencePanel />}
        {selection?.kind === "query" && (
          <QueryPanel domains={DOMAINS} onOpenFile={openFile} />
        )}
      </main>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="max-w-xl">
      <h1 className="text-lg font-semibold text-fg mb-2">Knowledge base</h1>
      <p className="text-sm text-fg-muted">
        Select a file in the tree to view or edit it. The Built-in tree holds the
        Executive&apos;s default playbooks (positive guidance) and failure case studies
        (negative learnings) — both are retrieved at chat time.
      </p>
      <ul className="text-sm text-fg-muted mt-4 space-y-1.5 list-disc list-inside">
        <li>
          <span className="text-fg">Playbooks</span> — domain frameworks and
          how-tos used as positive examples.
        </li>
        <li>
          <span className="text-rose-300">Failures</span> — case studies of what went
          wrong, surfaced when the question matches one strongly.
        </li>
        <li>
          <span className="text-fg">Company</span> — your uploaded documents.
        </li>
        <li>
          <span className="text-fg">Reference Library</span> — open-licensed
          textbooks and handbooks.
        </li>
        <li>
          <span className="text-fg">Review queue</span> — approve, reject, or
          correct knowledge before the Executive relies on it.
        </li>
        <li>
          <span className="text-indigo-300">Query mode</span> — see exactly what the
          Executive would retrieve for a question.
        </li>
      </ul>
    </div>
  );
}
