"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { BuiltinFileContent, ReviewItem, ReviewStatus } from "@/lib/api";
import ReviewStatusPill from "@/components/ReviewStatusPill";

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
  "prose prose-invert prose-sm max-w-none prose-p:text-fg prose-headings:text-fg prose-strong:text-fg prose-code:text-indigo-300 prose-code:bg-surface-overlay prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none prose-pre:bg-surface-overlay prose-pre:border prose-pre:border-line-strong prose-blockquote:border-line-strong prose-blockquote:text-fg-muted prose-ul:text-fg prose-ol:text-fg prose-li:marker:text-fg-muted prose-hr:border-line-strong prose-a:text-indigo-400 prose-a:no-underline hover:prose-a:underline prose-table:text-fg prose-th:text-fg prose-th:border-line-strong prose-td:border-line-strong";

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
  const accent = isFailure ? "text-rose-400" : "text-indigo-400";

  return (
    <div className="flex flex-col gap-3 h-full">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <span className={`text-xs font-semibold uppercase tracking-widest ${accent}`}>
            {isFailure ? "Failure · " : ""}
            {file.domain}
          </span>
          <h2 className="text-base font-semibold text-fg mt-0.5">{file.filename}</h2>
          {review && (
            <div className="flex items-center gap-2 mt-1">
              <ReviewStatusPill
                status={review.status}
                reviewedAt={review.reviewed_at}
                trustedDefault={review.trusted_default}
              />
              {review.status !== "approved" && (
                <button
                  onClick={() => onSetReviewStatus("approved")}
                  className="text-[11px] text-emerald-400 hover:text-emerald-300 transition-colors"
                >
                  Approve
                </button>
              )}
              {review.status !== "needs_revision" && (
                <button
                  onClick={() => onSetReviewStatus("needs_revision")}
                  className="text-[11px] text-violet-400 hover:text-violet-300 transition-colors"
                >
                  Flag
                </button>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className="flex gap-0.5 p-0.5 bg-surface-overlay rounded-lg border border-line-strong/50">
            {(["edit", "preview"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors capitalize ${
                  mode === m
                    ? "bg-surface-input text-fg"
                    : "text-fg-muted hover:text-fg"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
          <button
            onClick={onDelete}
            className="px-3 py-1.5 rounded-lg border border-red-500/20 text-red-400 text-xs hover:bg-red-500/10 transition-colors"
          >
            Delete
          </button>
          <button
            onClick={onSave}
            disabled={!isDirty || isSaving}
            className="px-3 py-1.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 disabled:opacity-40 text-white text-xs font-medium transition-colors"
          >
            {isSaving ? "Saving…" : isDirty ? "Save" : "Saved"}
          </button>
        </div>
      </div>

      {mode === "edit" ? (
        <textarea
          value={content}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 min-h-[520px] w-full rounded-xl border border-line-strong bg-surface-elevated px-4 py-3 text-sm text-fg font-mono leading-relaxed focus:outline-none focus:ring-2 focus:ring-indigo-500/50 resize-none"
          spellCheck={false}
        />
      ) : (
        <div
          className={`flex-1 h-[520px] rounded-xl border bg-surface-elevated px-6 py-5 overflow-y-auto ${PROSE_CLASS} ${
            isFailure ? "border-rose-900/40" : "border-line-strong"
          }`}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}
