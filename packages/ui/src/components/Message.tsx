"use client";

import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import BrandMark from "@/components/BrandMark";
import type { ActionTaken } from "@/lib/api";

interface MessageProps {
  role: "user" | "assistant";
  content: string;
  isStreaming?: boolean;
  // Inline action chips for assistant messages. Each chip represents a
  // side-effecting tool the Executive fired during this turn (DM sent,
  // workflow opened, person updated, alert flagged…). User messages
  // never have actions.
  actions?: ActionTaken[];
  // The user stopped this reply mid-stream. Renders a marker so a truncated
  // answer isn't read as a complete one — on reload too, since the flag is
  // persisted with the message.
  stopped?: boolean;
  // Explicit 👍/👎 on a persisted reply. Rendered only when `onFeedback` is
  // given (the reply has a stored id) and the reply has finished streaming.
  feedback?: "up" | "down" | null;
  onFeedback?: (value: "up" | "down" | null) => void;
}

function FeedbackButtons({
  value,
  onChange,
}: {
  value: "up" | "down" | null | undefined;
  onChange: (value: "up" | "down" | null) => void;
}) {
  const button = (kind: "up" | "down", glyph: string, label: string) => {
    const active = value === kind;
    return (
      <button
        type="button"
        aria-label={label}
        aria-pressed={active}
        title={label}
        onClick={() => onChange(active ? null : kind)}
        // An emoji ignores the text colour, so selection has to show some
        // other way: unselected ones are greyed out, the selected one is in
        // full colour on a ringed chip. Sized as a comfortable tap target.
        className={`min-w-[2rem] min-h-[2rem] px-2 rounded-md text-sm transition-all ${
          active
            ? "bg-indigo-500/15 ring-1 ring-indigo-400/70"
            : "grayscale opacity-50 hover:opacity-100 hover:grayscale-0"
        }`}
      >
        {glyph}
      </button>
    );
  };
  return (
    <div className="mt-2 flex items-center gap-1" aria-label="Rate this reply">
      {button("up", "👍", "Helpful")}
      {button("down", "👎", "Not helpful")}
      {value && (
        <span className="ml-1 text-xs text-fg-muted" role="status">
          {value === "up" ? "Marked helpful" : "Marked not helpful"}
        </span>
      )}
    </div>
  );
}

function ActionChip({ action }: { action: ActionTaken }) {
  const inner = (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] bg-emerald-500/10 border border-emerald-500/30 text-emerald-300">
      <span aria-hidden="true" className="text-[10px]">✓</span>
      <span>{action.summary}</span>
    </span>
  );
  if (action.link) {
    return (
      <Link href={action.link} className="hover:opacity-80 transition-opacity">
        {inner}
      </Link>
    );
  }
  return inner;
}

export default function Message({
  role,
  content,
  isStreaming,
  actions,
  stopped,
  feedback,
  onFeedback,
}: MessageProps) {
  if (role === "user") {
    return (
      <div className="flex justify-end mb-6">
        <div className="max-w-xl px-4 py-3 rounded-2xl rounded-tr-sm bg-surface-overlay text-fg text-sm leading-relaxed">
          <p className="whitespace-pre-wrap">{content}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex gap-3 sm:gap-4 mb-8">
      {/* Avatar */}
      <div className="flex-shrink-0 mt-1">
        <BrandMark size="md" />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="text-xs text-fg-muted mb-2 font-medium tracking-wide uppercase">Executive</div>
        <div className="prose prose-invert prose-sm max-w-none
          prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:bg-surface-overlay prose-code:before:content-none prose-code:after:content-none
          prose-pre:bg-surface-overlay prose-pre:border
          prose-a:text-accent prose-a:no-underline hover:prose-a:underline">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            urlTransform={(url) => {
              // Block javascript: and data: URL schemes to prevent XSS via prompt injection
              if (/^(javascript|data|vbscript):/i.test(url)) return "";
              return url;
            }}
          >
            {content}
          </ReactMarkdown>
          {isStreaming && (
            <span className="inline-block w-0.5 h-4 bg-accent cursor-blink ml-0.5 align-text-bottom rounded-full" />
          )}
        </div>

        {actions && actions.length > 0 && (
          <div
            className="mt-3 flex flex-wrap gap-1.5"
            aria-label={`${actions.length} action${actions.length === 1 ? "" : "s"} taken`}
          >
            {actions.map((action, i) => (
              <ActionChip key={`${action.tool}-${i}`} action={action} />
            ))}
          </div>
        )}

        {stopped && !isStreaming && (
          <p className="mt-2 text-xs text-fg-muted">Stopped by you</p>
        )}

        {onFeedback && !isStreaming && (
          <FeedbackButtons value={feedback} onChange={onFeedback} />
        )}
      </div>
    </div>
  );
}
