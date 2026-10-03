"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { BuiltinFileContent, ReviewItem, ReviewStatus } from "@/lib/api";
import ReviewStatusPill from "@/components/ReviewStatusPill";
import Button from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";

interface FileEditorProps {
  file: BuiltinFileContent;
  content: string;
  isDirty: boolean;
  isSaving: boolean;
  variant: "playbook" | "failure";
  /** The file's review record, or null when it has none. */
  review: ReviewItem | null;
  onSetReviewStatus: (status: ReviewStatus) => void;
  onChange: (v: string) => void;
  onSave: () => void;
  onDelete: () => void;
}

const PROSE_CLASS =
  "prose prose-sm sm:prose-base max-w-none prose-p:text-fg prose-headings:text-fg prose-strong:text-fg prose-code:text-accent prose-code:bg-surface-overlay prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none prose-pre:bg-surface-overlay prose-pre:border prose-pre:border-line-strong prose-blockquote:border-line-strong prose-blockquote:text-fg-muted prose-ul:text-fg prose-ol:text-fg prose-li:marker:text-fg-muted prose-hr:border-line-strong prose-a:text-accent prose-a:no-underline hover:prose-a:underline prose-table:text-fg prose-th:text-fg prose-th:border-line-strong prose-td:border-line-strong";

export default function FileEditor({
  file,
  content,
  isDirty,
  isSaving,
  variant,
  review,
  onSetReviewStatus,
  onChange,
  onSave,
  onDelete,
}: FileEditorProps) {
  const [mode, setMode] = useState<"edit" | "preview">("preview");
  const isFailure = variant === "failure";
  const accent = isFailure ? "text-rose-500" : "text-accent";

  // Approve stays on the card while the file is unapproved; the rarer review
  // flag and Delete sit in the ⋯ menu.
  const menu: OverflowItem[] = [];
  if (review && review.status !== "needs_revision") {
    menu.push({ label: "Flag for revision", onSelect: () => onSetReviewStatus("needs_revision") });
  }
  menu.push({ label: "Delete file", danger: true, onSelect: onDelete });

  return (
    <div className="flex flex-col gap-4 h-full">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <span className={`text-sm font-semibold capitalize ${accent}`}>
            {isFailure ? "Failure · " : ""}
            {file.domain}
          </span>
          <h2 className="text-lg sm:text-xl font-bold text-fg mt-0.5 break-words">{file.filename}</h2>
          {review && (
            <div className="flex items-center gap-2 mt-2">
              <ReviewStatusPill
                status={review.status}
                reviewedAt={review.reviewed_at}
                trustedDefault={review.trusted_default}
              />
              {review.status !== "approved" && (
                <Button size="sm" onClick={() => onSetReviewStatus("approved")}>
                  Approve
                </Button>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div
            role="group"
            aria-label="Mode"
            className="flex gap-1 p-1 bg-surface-overlay rounded-xl border border-line"
          >
            {(["edit", "preview"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                aria-pressed={mode === m}
                className={`h-9 px-3.5 rounded-lg text-sm font-medium transition-colors capitalize ${
                  mode === m ? "bg-surface-elevated text-fg shadow-sm" : "text-fg-muted hover:text-fg"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
          <Button variant="primary" onClick={onSave} disabled={!isDirty || isSaving}>
            {isSaving ? "Saving…" : isDirty ? "Save" : "Saved"}
          </Button>
          <OverflowMenu items={menu} label="More file actions" />
        </div>
      </div>

      {mode === "edit" ? (
        <textarea
          value={content}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 min-h-[520px] w-full rounded-2xl border border-line-strong bg-surface-elevated px-4 py-3 text-sm text-fg font-mono leading-relaxed focus:outline-none focus:ring-2 focus:ring-accent/50 resize-none"
          spellCheck={false}
        />
      ) : (
        <div
          className={`flex-1 min-h-[520px] rounded-2xl border bg-surface-elevated px-5 py-5 sm:px-6 ${PROSE_CLASS} ${
            isFailure ? "border-rose-500/30" : "border-line"
          }`}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}
