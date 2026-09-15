"use client";

import { useEffect, useRef, useState } from "react";
import {
  forceOnboardDraft,
  sendOnboardMessage,
  startOnboardInterview,
  type OnboardTurn,
} from "@/lib/api";

const MAX_FILES = 8;

interface Props {
  /** Prior turns when resuming a session, oldest first. */
  initialTurns?: { role: "user" | "assistant"; text: string }[];
  initialTurn: OnboardTurn | null;
  onDraft: (turn: OnboardTurn, turns: Bubble[]) => void;
}

export interface Bubble {
  role: "user" | "assistant";
  text: string;
}

export default function OnboardConversation({
  initialTurns = [],
  initialTurn,
  onDraft,
}: Props) {
  const [turn, setTurn] = useState<OnboardTurn | null>(initialTurn);
  const [bubbles, setBubbles] = useState<Bubble[]>(initialTurns);
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const started = turn !== null;

  // The header shows the current question. Drop it from the history only when
  // the last bubble IS that question — on resume from a draft the transcript
  // ends on the user's own message, which must stay visible.
  const headline = turn?.question ?? null;
  const earlier =
    headline && bubbles.length > 0 && bubbles[bubbles.length - 1].text === headline
      ? bubbles.slice(0, -1)
      : bubbles;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [bubbles, busy]);

  function applyTurn(next: OnboardTurn, sent: Bubble[]) {
    if (next.phase === "draft") {
      onDraft(next, [...bubbles, ...sent]);
      return;
    }
    setTurn(next);
    setBubbles((prev) => [
      ...prev,
      ...sent,
      { role: "assistant", text: next.question ?? "" },
    ]);
  }

  async function send() {
    const text = input.trim();
    if ((!text && files.length === 0) || busy) return;
    setBusy(true);
    setError(null);
    try {
      const sent: Bubble[] = text ? [{ role: "user", text }] : [];
      const next = started
        ? await sendOnboardMessage(turn!.session_id, text)
        : await startOnboardInterview(text, files);
      setInput("");
      setFiles([]);
      applyTurn(next, sent);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function draftNow() {
    if (!turn || busy) return;
    setBusy(true);
    setError(null);
    try {
      onDraft(await forceOnboardDraft(turn.session_id), bubbles);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-4">
        <div className="bg-surface-elevated border border-line rounded-xl px-5 py-4">
          <p className="text-sm text-fg whitespace-pre-wrap">
            {headline ??
              (started
                ? "Anything else I should know? Tell me what to change, or draft again."
                : "Tell me about your company — what you do, who you sell to, roughly how big you are, and what you're focused on this year. Write it however you like; I'll ask about anything I'm missing. You can also attach a deck, a one-pager, or anything else that describes the business.")}
          </p>
          {turn?.question_hint && (
            <p className="text-xs text-fg-muted mt-2">{turn.question_hint}</p>
          )}
        </div>

        {earlier.length > 0 && (
          <details className="text-xs text-fg-muted">
            <summary className="cursor-pointer hover:text-fg transition-colors">
              Earlier in this conversation ({earlier.length}{" "}
              {earlier.length === 1 ? "message" : "messages"})
            </summary>
            <div className="flex flex-col gap-3 mt-3">
              {earlier.map((b, i) => (
                <div key={i} className={b.role === "user" ? "pl-6" : ""}>
                  <p className="text-[10px] uppercase tracking-wide text-fg-subtle mb-0.5">
                    {b.role === "user" ? "You" : "Setup"}
                  </p>
                  <p className="text-xs text-fg whitespace-pre-wrap">{b.text}</p>
                </div>
              ))}
            </div>
          </details>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          rows={started ? 3 : 7}
          disabled={busy}
          autoFocus
          placeholder={
            started
              ? "Your answer… (Enter to send, Shift+Enter for a new line)"
              : "e.g. We're Northwind Tools — we sell industrial supplies to construction crews in the Midwest. About 40 people, bootstrapped, doing roughly $12M a year. This year we're trying to launch same-day delivery without blowing up margins."
          }
          className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 resize-none transition-colors disabled:opacity-50"
        />

        {!started && (
          <div className="flex items-center gap-3">
            <label className="text-xs text-indigo-400 hover:text-indigo-300 cursor-pointer transition-colors">
              Attach files
              <input
                type="file"
                multiple
                className="hidden"
                accept=".pdf,.docx,.doc,.xlsx,.xlsm,.csv,.md,.txt"
                onChange={(e) =>
                  setFiles(Array.from(e.target.files ?? []).slice(0, MAX_FILES))
                }
              />
            </label>
            {files.length > 0 && (
              <p className="text-xs text-fg-muted">
                {files.map((f) => f.name).join(", ")}
              </p>
            )}
          </div>
        )}

        {error && <p className="text-xs text-red-400">{error}</p>}

        <div className="flex items-center gap-3 mt-1">
          <button
            onClick={() => void send()}
            disabled={busy || (!input.trim() && files.length === 0)}
            className="px-4 py-2 bg-indigo-500 hover:bg-indigo-600 disabled:opacity-40 text-white text-sm font-medium rounded-lg transition-colors"
          >
            {busy ? "Thinking…" : started ? "Send" : "Get started"}
          </button>
          {started && (
            <button
              onClick={() => void draftNow()}
              disabled={busy}
              className="text-xs text-fg-muted hover:text-fg disabled:opacity-40 transition-colors"
            >
              Skip ahead — draft my profile now
            </button>
          )}
          {started && (
            <span className="text-xs text-fg-subtle ml-auto">
              {turn!.questions_asked} of {turn!.max_questions} questions
            </span>
          )}
        </div>
      </div>

      <div ref={bottomRef} />
    </div>
  );
}
