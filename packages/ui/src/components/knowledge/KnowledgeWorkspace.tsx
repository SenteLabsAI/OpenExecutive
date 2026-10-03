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
import PageSideNav from "@/components/shell/PageSideNav";
import Button from "@/components/ui/Button";
import SectionTabs from "@/components/ui/SectionTabs";
import {
  ADVANCED_VIEWS,
  VIEW_LABELS,
  isAdvancedView,
  viewForSelection,
  viewFromParam,
  type KnowledgeView,
} from "@/lib/knowledgeViews";
import CompanyPanel from "./CompanyPanel";
import FileEditor from "./FileEditor";
import NewFileForm from "./NewFileForm";
import QueryPanel from "./QueryPanel";
import ReferencePanel from "./ReferencePanel";
import SourceTree, { type FileKind, type Selection } from "./SourceTree";

// What the phone bar above the playbooks tree names as open.
function fileLabel(selection: Selection): string | undefined {
  if (selection?.kind === "file") return selection.filename;
  if (selection?.kind === "new") return "New file";
  return undefined;
}

function selectionForView(view: KnowledgeView): Selection {
  return { kind: view };
}

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
  // here) opens straight onto the review queue; `?view=` also takes the other
  // Advanced views. Everything else opens on the company documents.
  const [selection, setSelection] = useState<Selection>(() =>
    selectionForView(viewFromParam(searchParams.get("view")))
  );
  const [companyCount, setCompanyCount] = useState<number | null>(null);
  const [reviewCount, setReviewCount] = useState(0);
  const [fileReview, setFileReview] = useState<ReviewItem | null>(null);
  // Bumped on every review-status request; only the latest may write, so a
  // slow response (another file, or a refetch racing an Approve) can't
  // overwrite a newer one.
  const reviewSeq = useRef(0);

  // Also follow `?view=` on in-app navigation, where the page is not
  // remounted and the initializer above doesn't run again.
  const viewParam = searchParams.get("view");
  useEffect(() => {
    const wanted = viewFromParam(viewParam);
    if (isAdvancedView(wanted)) setSelection(selectionForView(wanted));
  }, [viewParam]);
  const view = viewForSelection(selection?.kind);
  // The Advanced view last open, so "Advanced" returns to it.
  const [lastAdvanced, setLastAdvanced] = useState<KnowledgeView>(
    isAdvancedView(view) ? view : "review"
  );
  if (isAdvancedView(view) && view !== lastAdvanced) setLastAdvanced(view);
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
      setSelection({ kind: "playbooks" });
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

  function openView(next: KnowledgeView) {
    if (next === view) return;
    setSelection(selectionForView(next));
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6 sm:px-8 sm:py-8">
        <div className="mb-6">
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Knowledge</h1>
          <p className="mt-1.5 text-[15px] text-fg-muted max-w-2xl">
            Everything here is read by the Executive when it answers questions about your
            company.
          </p>
        </div>

        <SectionTabs
          label="Knowledge"
          active={isAdvancedView(view) ? "advanced" : "company"}
          onChange={(t) => openView(t === "advanced" ? lastAdvanced : "company")}
          tabs={[
            { id: "company", label: VIEW_LABELS.company, badge: companyCount },
            {
              id: "advanced",
              label: "Advanced",
              // The review count rides on "Advanced" so pending items show
              // from the documents view too.
              badge: !isAdvancedView(view) && reviewCount > 0 ? reviewCount : null,
              badgeTone: "attention",
            },
          ]}
        />

        {isAdvancedView(view) && (
          <div className="mt-3 flex items-center gap-1 overflow-x-auto border-b border-line">
            {ADVANCED_VIEWS.map((v) => {
              const active = v === view;
              return (
                <button
                  key={v}
                  onClick={() => openView(v)}
                  aria-current={active ? "page" : undefined}
                  className={`-mb-px inline-flex h-11 flex-shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 text-[15px] font-medium transition-colors ${
                    active
                      ? "border-accent text-fg"
                      : "border-transparent text-fg-muted hover:text-fg"
                  }`}
                >
                  {VIEW_LABELS[v]}
                  {v === "review" && reviewCount > 0 && (
                    <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-semibold tabular-nums text-amber-500">
                      {reviewCount}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}

        {error && (
          <div className="mt-5 text-sm text-red-500 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3">
            {error}
          </div>
        )}

        <div className="mt-6">
          {view === "company" && <CompanyPanel onCountChange={setCompanyCount} />}
          {view === "review" && <ReviewQueue />}
          {view === "reference" && <ReferencePanel />}
          {view === "query" && <QueryPanel domains={DOMAINS} onOpenFile={openFile} />}
          {view === "playbooks" && (
            <div className="flex flex-col md:flex-row md:gap-6">
              <PageSideNav
                label="File"
                current={fileLabel(selection)}
                closeKey={JSON.stringify(selection)}
                className="md:w-64 md:max-h-[calc(100vh-14rem)] md:sticky md:top-0 bg-surface-elevated md:bg-transparent p-4 md:p-0 md:pr-4"
              >
                <SourceTree
                  domains={DOMAINS}
                  builtinFiles={builtinFiles}
                  failureFiles={failureFiles}
                  selection={selection}
                  filter={filter}
                  onFilterChange={setFilter}
                  onSelect={setSelection}
                />
              </PageSideNav>
              <div className="min-w-0 flex-1 pt-4 md:pt-0">
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
                  <p className="text-[15px] text-fg-muted">Loading…</p>
                )}
                {selection?.kind === "new" && (
                  <NewFileForm
                    domains={DOMAINS}
                    initialDomain={DOMAINS[0]}
                    variant={selection.fileKind === "failures" ? "failure" : "playbook"}
                    onSave={(domain, filename, content) =>
                      handleCreate(selection.fileKind, domain, filename, content)
                    }
                    onCancel={() => setSelection({ kind: "playbooks" })}
                  />
                )}
                {selection?.kind === "playbooks" && (
                  <div className="rounded-2xl border border-dashed border-line-strong px-6 py-12 text-center">
                    <p className="text-base font-semibold text-fg">Built-in playbooks</p>
                    <p className="mt-1.5 text-[15px] text-fg-muted max-w-md mx-auto">
                      The frameworks and failure case studies each specialist draws on.
                      Pick a file to read or edit it, or add your own.
                    </p>
                    <Button
                      variant="primary"
                      className="mt-5"
                      onClick={() => setSelection({ kind: "new", fileKind: "builtin" })}
                    >
                      New playbook
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
