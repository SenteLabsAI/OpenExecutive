"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { WORKFLOW_STARTERS, stashWorkflowDescription } from "@/lib/workflowStarters";

const CHAT_DRAFT = "Help me set up a workflow that ";

/**
 * The front door of /jobs: ask what the user wants done, and offer the ways
 * to build it — describe it (the wizard), step by step (the advanced
 * editor), in chat, or from a ready-made workflow further down the page.
 */
export default function StartHere({ onBrowseReadyMade }: { onBrowseReadyMade: () => void }) {
  const router = useRouter();
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const start = () => {
    const message = text.trim();
    if (!message) return;
    router.push(
      stashWorkflowDescription(message)
        ? "/jobs/new"
        : `/jobs/new?describe=${encodeURIComponent(message)}`
    );
  };

  return (
    <section className="mb-6 rounded-xl border border-line bg-surface-elevated p-5 sm:p-6">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-indigo-300">
        Start here
      </p>
      <h2 className="mt-1 text-lg font-semibold text-fg">What do you want to get done?</h2>
      <p className="mt-1 text-sm text-fg-muted">
        A workflow is a job you want done regularly or on demand — a report, a
        review, a check-in. Describe it in a sentence; I&rsquo;ll ask a few
        questions and set it up for you to review.
      </p>

      <div className="mt-4">
        <textarea
          ref={inputRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              start();
            }
          }}
          rows={2}
          aria-label="Describe the workflow you want"
          placeholder="e.g. Every Monday, send me what our top competitors shipped"
          className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 resize-none transition-colors"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={start}
            disabled={!text.trim()}
            className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40 transition-colors"
          >
            Start
          </button>
          <span className="text-xs text-fg-subtle">or try one:</span>
          {WORKFLOW_STARTERS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => {
                setText(s);
                inputRef.current?.focus();
              }}
              className="rounded-full border border-line px-3 py-1 text-left text-xs text-fg-muted hover:text-fg hover:border-line-strong transition"
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      <p className="mt-5 mb-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
        Or build it another way
      </p>
      <div className="grid gap-2 sm:grid-cols-3">
        <WayCard
          href="/jobs/new?mode=advanced"
          title="Step by step"
          body="Pick each step yourself: which specialist, which tools, where you sign off."
        />
        <WayCard
          href={`/?new=1&draft=${encodeURIComponent(CHAT_DRAFT)}`}
          title="Talk it through in chat"
          body="Explain it to the Executive in your own words and it will draft the workflow."
        />
        <button
          type="button"
          onClick={onBrowseReadyMade}
          className="rounded-lg border border-line bg-surface/40 px-3 py-2.5 text-left hover:border-line-strong hover:bg-surface-elevated/40 transition"
        >
          <span className="block text-sm font-medium text-fg">Start from a ready-made one</span>
          <span className="mt-0.5 block text-xs text-fg-muted">
            Board decks, investor updates, business reviews and more — run as-is.
          </span>
        </button>
      </div>
    </section>
  );
}

function WayCard({ href, title, body }: { href: string; title: string; body: string }) {
  return (
    <Link
      href={href}
      className="rounded-lg border border-line bg-surface/40 px-3 py-2.5 hover:border-line-strong hover:bg-surface-elevated/40 transition"
    >
      <span className="block text-sm font-medium text-fg">{title}</span>
      <span className="mt-0.5 block text-xs text-fg-muted">{body}</span>
    </Link>
  );
}
