"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import { WORKFLOW_STARTER_CHIPS, stashWorkflowDescription } from "@/lib/workflowStarters";

const CHAT_DRAFT = "Help me set up a workflow that ";

const chipCls =
  "inline-flex min-h-10 items-center rounded-full border border-line bg-surface-elevated px-4 text-sm font-medium text-fg-muted hover:text-fg hover:border-line-strong hover:bg-surface-overlay transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60";

/**
 * The front door of /jobs: ask what the user wants done. Start hands the
 * sentence to the wizard; the ⋯ beside it holds the other ways to build one
 * (the wizard empty, the step-by-step editor, chat), and the last chip opens
 * the ready-made list.
 */
export default function StartHere({
  readyMadeCount,
  onBrowseReadyMade,
}: {
  readyMadeCount: number;
  onBrowseReadyMade: () => void;
}) {
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
    <section className="mb-8 rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm sm:p-7">
      <h1 className="text-2xl font-bold tracking-tight text-fg sm:text-3xl">
        What do you want to get done?
      </h1>
      <p className="mt-2 text-[15px] text-fg-muted">
        A report, a review, a check-in — regularly or on demand. Describe it in a
        sentence; I&rsquo;ll ask a few questions and set it up for you to review.
      </p>

      <div className="mt-5 flex flex-col gap-3 rounded-2xl border border-line bg-surface p-2 focus-within:ring-2 focus-within:ring-accent/40 sm:flex-row sm:items-end">
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
          className="min-h-[3.5rem] w-full flex-1 resize-none bg-transparent px-3 py-2.5 text-base text-fg placeholder-fg-subtle focus:outline-none"
        />
        <div className="flex items-center justify-end gap-1.5">
          <Button variant="primary" onClick={start} disabled={!text.trim()} className="px-6">
            Start
          </Button>
          <OverflowMenu
            label="Other ways to build a workflow"
            items={[
              { label: "New workflow", href: "/jobs/new" },
              { label: "New workflow, step by step", href: "/jobs/new?mode=advanced" },
              {
                label: "Talk it through in chat",
                href: `/?new=1&draft=${encodeURIComponent(CHAT_DRAFT)}`,
              },
            ]}
          />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {WORKFLOW_STARTER_CHIPS.map((s) => (
          <button
            key={s.label}
            type="button"
            title={s.text}
            onClick={() => {
              setText(s.text);
              inputRef.current?.focus();
            }}
            className={chipCls}
          >
            {s.label}
          </button>
        ))}
        <button type="button" onClick={onBrowseReadyMade} className={chipCls}>
          {readyMadeCount > 0 ? `Browse ${readyMadeCount} ready-made` : "Browse ready-made"}
        </button>
      </div>
    </section>
  );
}
