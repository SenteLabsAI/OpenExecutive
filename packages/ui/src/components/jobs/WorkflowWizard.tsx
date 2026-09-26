"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Person,
  WorkflowDesignerTurn,
  forceWorkflowDesignerDraft,
  getWorkflowDesignerSession,
  listPeople,
  sendWorkflowDesignerMessage,
  startWorkflowDesigner,
} from "@/lib/api";
import { WORKFLOW_STARTERS, takeWorkflowDescription } from "@/lib/workflowStarters";
import WorkflowDraftReview from "./WorkflowDraftReview";

// Same cap chat applies to its `?draft=` prefill.
const MAX_DESCRIBE_PARAM_CHARS = 2000;

/**
 * Conversational "New workflow": describe the job, answer a few clarifying
 * questions, review the draft, create it. The thread scrolls inside a
 * fixed-height panel with the composer pinned, so the page never grows.
 */
export default function WorkflowWizard() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const resumeId = searchParams.get("session");

  const [turn, setTurn] = useState<WorkflowDesignerTurn | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [resuming, setResuming] = useState(!!resumeId);
  const [error, setError] = useState<string | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // The session this component already holds, so mirroring it into the URL
  // does not trigger a redundant resume fetch.
  const heldSessionRef = useRef<string | null>(null);

  useEffect(() => {
    listPeople()
      .then(setPeople)
      .catch(() => setPeople([]));
  }, []);

  // Resume after a refresh, or on "Back" from the details editor.
  useEffect(() => {
    if (!resumeId || resumeId === heldSessionRef.current) return;
    getWorkflowDesignerSession(resumeId)
      .then((t) => {
        heldSessionRef.current = t.session_id;
        setTurn(t);
      })
      .catch(() => router.replace(pathname, { scroll: false }))
      .finally(() => setResuming(false));
  }, [resumeId, pathname, router]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turn, pending, busy]);

  const run = useCallback(
    async (op: () => Promise<WorkflowDesignerTurn>, pendingText: string | null) => {
      setError(null);
      setBusy(true);
      setPending(pendingText);
      try {
        const next = await op();
        setTurn(next);
        setInput("");
        if (next.session_id !== heldSessionRef.current) {
          heldSessionRef.current = next.session_id;
          router.replace(`${pathname}?session=${encodeURIComponent(next.session_id)}`, {
            scroll: false,
          });
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setPending(null);
        setBusy(false);
        inputRef.current?.focus();
      }
    },
    [pathname, router]
  );

  // Arriving from the "Start here" panel on /jobs: send what the user typed
  // there as the first message. A `?describe=` link only fills the composer.
  // The ref keeps StrictMode's second effect run from starting a second session.
  const describeParam = searchParams.get("describe");
  const handoffRef = useRef(false);
  useEffect(() => {
    if (handoffRef.current) return;
    handoffRef.current = true;
    // Always consumed, so a leftover never fires on a later visit.
    const handedOff = takeWorkflowDescription()?.trim();
    if (resumeId) return;
    if (handedOff) {
      // Kept in the composer until the first turn succeeds, so a failed
      // start leaves the text ready to retry.
      setInput(handedOff);
      void run(() => startWorkflowDesigner(handedOff), handedOff);
      return;
    }
    if (describeParam) {
      setInput(describeParam.slice(0, MAX_DESCRIBE_PARAM_CHARS));
      router.replace(pathname, { scroll: false });
    }
  }, [resumeId, describeParam, run, router, pathname]);

  const send = (text: string) => {
    const message = text.trim();
    if (!message || busy) return;
    void run(
      () =>
        turn
          ? sendWorkflowDesignerMessage(turn.session_id, message)
          : startWorkflowDesigner(message),
      message
    );
  };

  const draftNow = () => {
    if (!turn || busy) return;
    void run(() => forceWorkflowDesignerDraft(turn.session_id), null);
  };

  if (resuming) {
    return <div className="p-6 text-sm text-fg-muted">Loading…</div>;
  }

  const started = turn !== null;
  const isDraft = turn?.phase === "draft" && turn.draft !== null;
  // In the draft phase the last assistant turn is the draft's summary; the
  // review card renders it, so the thread stops one turn short.
  const thread = turn
    ? isDraft
      ? turn.transcript.slice(0, -1)
      : turn.transcript
    : [];
  const lastIndex = thread.length - 1;

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex-1 min-h-0 overflow-y-auto px-6 py-6">
        <div className="max-w-3xl mx-auto space-y-4">
          {!started && (
            <div className="space-y-3">
              <p className="text-sm text-fg">
                Describe the job you want done — what it should produce, who it&rsquo;s
                for, and how often. I&rsquo;ll ask about anything I need, then draft the
                workflow for you to review.
              </p>
              <div className="flex flex-wrap gap-2">
                {WORKFLOW_STARTERS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => {
                      setInput(s);
                      inputRef.current?.focus();
                    }}
                    className="rounded-full border border-line px-3 py-1 text-left text-xs text-fg-muted hover:text-fg hover:border-line-strong transition"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {thread.map((t, i) => (
            <div
              key={i}
              className={t.role === "user" ? "flex justify-end" : "flex justify-start"}
            >
              <div
                className={`max-w-[85%] rounded-xl px-4 py-2.5 text-sm whitespace-pre-wrap ${
                  t.role === "user"
                    ? "bg-indigo-600/20 text-fg"
                    : "bg-surface-elevated border border-line text-fg"
                }`}
              >
                {t.text}
                {i === lastIndex && t.role === "assistant" && turn?.hint && (
                  <p className="text-xs text-fg-muted mt-1.5">{turn.hint}</p>
                )}
              </div>
            </div>
          ))}

          {!isDraft && !busy && turn && turn.options.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {turn.options.map((o) => (
                <button
                  key={o}
                  type="button"
                  onClick={() => send(o)}
                  className="rounded-full border border-indigo-500/40 px-3 py-1 text-xs text-indigo-300 hover:bg-indigo-500/10 transition"
                >
                  {o}
                </button>
              ))}
            </div>
          )}

          {isDraft && turn && turn.draft && (
            <WorkflowDraftReview
              draft={turn.draft}
              sessionId={turn.session_id}
              people={people}
              busy={busy}
              onRefine={() => inputRef.current?.focus()}
            />
          )}

          {pending && (
            <div className="flex justify-end">
              <div className="max-w-[85%] rounded-xl px-4 py-2.5 text-sm whitespace-pre-wrap bg-indigo-600/20 text-fg opacity-70">
                {pending}
              </div>
            </div>
          )}
          {busy && <p className="text-xs text-fg-muted">Thinking…</p>}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="border-t border-line bg-surface px-6 py-3">
        <div className="max-w-3xl mx-auto space-y-2">
          {error && <p className="text-xs text-red-400">{error}</p>}
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            rows={started ? 2 : 4}
            disabled={busy}
            autoFocus
            placeholder={
              isDraft
                ? "Tell me what to change… (Enter to send)"
                : started
                ? "Your answer… (Enter to send, Shift+Enter for a new line)"
                : "e.g. Every Monday, pull together what our top three competitors shipped and send it to me."
            }
            className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 resize-none transition-colors disabled:opacity-50"
          />
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => send(input)}
              disabled={busy || !input.trim()}
              className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium rounded-md transition-colors"
            >
              {busy ? "Thinking…" : started ? "Send" : "Start"}
            </button>
            {started && !isDraft && (
              <button
                type="button"
                onClick={draftNow}
                disabled={busy}
                className="text-xs text-fg-muted hover:text-fg disabled:opacity-40 transition-colors"
              >
                Draft it now
              </button>
            )}
            <span className="ml-auto flex items-center gap-3 text-xs text-fg-subtle">
              {started && (
                <span>
                  {turn!.questions_asked} of {turn!.max_questions} questions
                </span>
              )}
              <Link href="/jobs/new?mode=advanced" className="hover:text-fg transition-colors">
                Use the advanced editor
              </Link>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
