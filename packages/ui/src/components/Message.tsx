"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import AnswerSourcesFooter from "@/components/AnswerSourcesFooter";
import BrandMark from "@/components/BrandMark";
import type { AnswerSources } from "@/lib/answerSources";
import type { ActionTaken } from "@/lib/api";
import { groupActions, opensDetails, type ChipGroup } from "@/lib/actionChips";
import { loadsInline } from "@/lib/markdownImages";
import { isMailboxLink } from "@/lib/replyCards";
import { hostOf } from "@/lib/url";
import FeatureName from "@/components/FeatureName";

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
  // What the reply looked at, and any part of the analysis it had to leave
  // out. Shown once the reply has finished streaming.
  sources?: AnswerSources;
  // Explicit 👍/👎 on a persisted reply. Rendered only when `onFeedback` is
  // given (the reply has a stored id) and the reply has finished streaming.
  feedback?: "up" | "down" | null;
  onFeedback?: (value: "up" | "down" | null) => void;
  // Shown under the text while the reply streams, e.g. the "still working"
  // line when the Executive goes quiet mid-reply to run a tool.
  status?: ReactNode;
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

function ChipBody({ summary, count }: { summary: string; count: number }) {
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] bg-emerald-500/10 border border-emerald-500/30 text-emerald-300">
      <span aria-hidden="true" className="text-[10px]">✓</span>
      <span>{summary}</span>
      {count > 1 && (
        <span className="rounded-full bg-emerald-400/20 px-1.5 font-semibold tabular-nums">
          ×{count}
        </span>
      )}
    </span>
  );
}

function ChipLink({ link, children }: { link: string; children: ReactNode }) {
  // A draft in your own mailbox (Act as me) opens in a new tab; every other
  // chip links inside the app.
  if (isMailboxLink(link)) {
    return (
      <a
        href={link}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 hover:opacity-80 transition-opacity"
      >
        <FeatureName feature="act_as_me" className="text-[11px]" />
        {children}
      </a>
    );
  }
  return (
    <Link href={link} className="hover:opacity-80 transition-opacity">
      {children}
    </Link>
  );
}

// What each run of a chip looked at. A bottom sheet on a phone, a card under
// the chips from the tablet breakpoint up.
function ChipDetails({ group, onClose }: { group: ChipGroup; onClose: () => void }) {
  const count = group.runs.length;
  const rows = group.runs.filter((run) => run.target || run.link);
  return (
    <>
      <div
        aria-hidden="true"
        onClick={onClose}
        className="fixed inset-0 z-40 bg-black/40 sm:hidden"
      />
      <div
        role="dialog"
        aria-label={group.summary}
        className="fixed inset-x-0 bottom-0 z-50 rounded-t-2xl border-t border-border bg-surface-elevated p-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))] shadow-2xl sm:static sm:z-auto sm:mt-2 sm:max-w-sm sm:rounded-xl sm:border sm:p-3 sm:shadow-lg"
      >
        <div aria-hidden="true" className="mx-auto mb-3 h-1 w-10 rounded-full bg-fg-muted/40 sm:hidden" />
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm font-semibold text-fg">
            {group.summary}
            {count > 1 && <span className="font-normal text-fg-muted"> · {count} times</span>}
          </p>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="min-h-[2rem] min-w-[2rem] rounded-md text-fg-muted hover:text-fg"
          >
            ✕
          </button>
        </div>
        {rows.length > 0 && (
          <ul className="mt-2 divide-y divide-border text-sm">
            {rows.map((run, i) => (
              <li key={i} className="py-2 break-words">
                {run.link ? (
                  <ChipLink link={run.link}>
                    <span className="text-accent">{run.target || "Open"}</span>
                  </ChipLink>
                ) : (
                  <span className="text-fg">{run.target}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

function ActionChips({ actions }: { actions: ActionTaken[] }) {
  const groups = useMemo(() => groupActions(actions), [actions]);
  const [open, setOpen] = useState<string | null>(null);
  const openGroup = groups.find((g) => g.key === open) ?? null;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="mt-3">
      <div
        className="flex flex-wrap gap-1.5"
        aria-label={`${actions.length} action${actions.length === 1 ? "" : "s"} taken`}
      >
        {groups.map((group) => {
          const body = <ChipBody summary={group.summary} count={group.runs.length} />;
          if (opensDetails(group)) {
            const isOpen = open === group.key;
            return (
              <button
                key={group.key}
                type="button"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : group.key)}
                className={`rounded-full transition-opacity hover:opacity-80 ${
                  isOpen ? "ring-2 ring-emerald-400/70 ring-offset-1 ring-offset-surface" : ""
                }`}
              >
                {body}
              </button>
            );
          }
          const link = group.runs[0]?.link;
          return link ? (
            <ChipLink key={group.key} link={link}>
              {body}
            </ChipLink>
          ) : (
            <span key={group.key}>{body}</span>
          );
        })}
      </div>
      {openGroup && <ChipDetails group={openGroup} onClose={() => setOpen(null)} />}
    </div>
  );
}

export default function Message({
  role,
  content,
  isStreaming,
  actions,
  stopped,
  sources,
  feedback,
  onFeedback,
  status,
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
      {/* Avatar. Hidden on phones, where its column would take an eighth of
          the width the answer needs. */}
      <div className="hidden sm:block flex-shrink-0 mt-1">
        <BrandMark size="md" />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="text-xs text-fg-muted mb-2 font-medium tracking-wide uppercase">Executive</div>
        <div className="prose prose-invert prose-sm max-w-none
          prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:bg-surface-overlay prose-code:before:content-none prose-code:after:content-none
          prose-pre:bg-surface-overlay prose-pre:text-fg prose-pre:border
          prose-a:text-accent prose-a:no-underline hover:prose-a:underline">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            urlTransform={(url) => {
              // Block javascript: and data: URL schemes to prevent XSS via prompt injection
              if (/^(javascript|data|vbscript):/i.test(url)) return "";
              return url;
            }}
            components={{
              // An image from another site never loads by itself: its URL
              // could carry what the reply quotes (see loadsInline).
              img: ({ src, alt }) =>
                loadsInline(src) ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={src as string} alt={alt ?? ""} />
                ) : typeof src === "string" && src ? (
                  <a href={src} target="_blank" rel="noopener noreferrer nofollow">
                    {alt || "Image"} ({hostOf(src)})
                  </a>
                ) : null,
            }}
          >
            {content}
          </ReactMarkdown>
          {isStreaming && (
            <span className="inline-block w-0.5 h-4 bg-accent cursor-blink ml-0.5 align-text-bottom rounded-full" />
          )}
        </div>

        {status && <div className="mt-3">{status}</div>}

        {actions && actions.length > 0 && <ActionChips actions={actions} />}

        {sources && !isStreaming && <AnswerSourcesFooter sources={sources} />}

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
