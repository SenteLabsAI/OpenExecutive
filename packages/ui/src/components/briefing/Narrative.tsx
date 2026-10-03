"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { MEMORY_ACTIONS, briefingMemoryLine } from "@/lib/briefing-memory";

import { PanelIntro, buildNarrativeSeed, type ContinueHandler } from "./shared";

// Flatten a react-markdown AST node to its plain text. Bullets in the
// "What's going on" narrative are `**Headline** — text`; this reads the
// underlying text nodes (recursing through bold/em/links) so a clicked bullet
// can be handed to the Executive as one clean string.
function nodeToPlainText(node: unknown): string {
  if (!node || typeof node !== "object") return "";
  const n = node as { value?: string; children?: unknown[] };
  if (typeof n.value === "string") return n.value;
  if (Array.isArray(n.children)) return n.children.map(nodeToPlainText).join("");
  return "";
}

// "What's going on" — the Executive's read on things right now, shown in the
// side panel "Read today's brief" opens. While the first one is still being
// written (`narrative` empty, `stale` true) it says so.
export default function NarrativeBody({
  narrative,
  stale,
  solo,
  onContinue,
}: {
  narrative: string | null | undefined;
  stale: boolean;
  solo: boolean;
  onContinue?: ContinueHandler;
}) {
  if (!narrative) {
    return (
      <div aria-live="polite" className="rounded-xl bg-accent/5 px-4 py-3 text-[15px] text-fg-muted animate-pulse">
        {stale ? "Catching up on today…" : "No brief yet today."}
      </div>
    );
  }
  return (
    <>
      <PanelIntro>
        The Executive&apos;s read on {solo ? "your work" : "the company"} right now — what came in today, what&apos;s
        stuck, what&apos;s next on your calendar, plus proposals and at-risk goals. Rewritten as the picture changes.
        {onContinue && " Tap a point to talk it through."}
      </PanelIntro>
      <div className="prose prose-invert max-w-none text-[15px] prose-p:my-2 prose-ul:my-2 prose-headings:text-fg prose-strong:text-fg">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={
            onContinue
              ? {
                  // Drop the default disc + marker padding so 💬 acts as the
                  // bullet. Tailwind utilities beat the `prose` plugin's
                  // zero-specificity :where(ul) rules, so list-none/pl-0 win.
                  ul: ({ children }) => <ul className="list-none pl-0 my-2 space-y-1">{children}</ul>,
                  // Each narrative bullet is its own click target → hands off
                  // to chat to discuss that item. The bottom-line and "Move
                  // today:" lines are paragraphs/strong text, so they stay
                  // non-interactive.
                  li: ({ node, children }) => {
                    const text = nodeToPlainText(node).trim();
                    // No extractable text (empty bullet, or a react-markdown
                    // version that doesn't forward `node`) → a plain item
                    // rather than a button that seeds an empty prompt.
                    if (!text) return <li className="list-none">{children}</li>;
                    return (
                      <li className="list-none pl-0 my-0">
                        <button
                          type="button"
                          onClick={() =>
                            onContinue(buildNarrativeSeed(text), briefingMemoryLine(MEMORY_ACTIONS.narrative, text))
                          }
                          aria-label={`Discuss: ${text}`}
                          className="group -mx-2 flex w-full items-start gap-2.5 rounded-xl px-2 py-2 text-left cursor-pointer transition hover:bg-accent/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                        >
                          {/* 💬 stands in for the bullet and signals "tap to
                              discuss"; it lifts on hover as the click cue. */}
                          <span aria-hidden className="mt-0.5 flex-shrink-0 select-none opacity-70 transition group-hover:opacity-100">
                            💬
                          </span>
                          <span className="min-w-0">{children}</span>
                        </button>
                      </li>
                    );
                  },
                }
              : undefined
          }
        >
          {narrative}
        </ReactMarkdown>
      </div>
    </>
  );
}
