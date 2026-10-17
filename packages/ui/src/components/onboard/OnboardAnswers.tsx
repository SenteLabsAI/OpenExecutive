"use client";

import { useState } from "react";
import {
  getOnboardInterview,
  reviseOnboardAnswer,
  type OnboardTurn,
} from "@/lib/api";
import type { Bubble } from "@/components/onboard/OnboardConversation";

// Where an attached file's text starts in the opening answer (the API's
// ATTACHED_MARKER). Only what was typed is shown and edited; the files stay.
const ATTACHED_MARKER = "\n\n=== Attached: ";

function typedPart(text: string): { typed: string; attached: boolean } {
  const cut = text.indexOf(ATTACHED_MARKER);
  return cut === -1
    ? { typed: text, attached: false }
    : { typed: text.slice(0, cut), attached: true };
}

interface Props {
  sessionId: string;
  bubbles: Bubble[];
  /** The interview after an edit: the transcript from the server and the new turn. */
  onRevised: (turn: OnboardTurn, bubbles: Bubble[]) => void;
  /** Draft screen: answers only, without the questions between them. */
  compact?: boolean;
  disabled?: boolean;
}

// The person's answers so far, each with Edit. Saving an edit drops what
// came after it and the Executive carries on from there: the next question,
// or a fresh draft when it already has enough.
export default function OnboardAnswers({
  sessionId,
  bubbles,
  onRevised,
  compact = false,
  disabled = false,
}: Props) {
  const [editing, setEditing] = useState<number | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Which of the person's answers each bubble is (0 = the description).
  let n = -1;
  const rows = bubbles.map((b, i) => ({
    ...b,
    i,
    answer: b.role === "user" ? ++n : -1,
  }));
  const shown = compact ? rows.filter((r) => r.role === "user") : rows;
  if (shown.length === 0) return null;

  async function save(answer: number) {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await reviseOnboardAnswer(sessionId, answer, text);
      const session = await getOnboardInterview(sessionId);
      setEditing(null);
      onRevised(next, session.turns);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const editAt = editing === null ? -1 : rows.find((r) => r.answer === editing)?.i ?? -1;

  return (
    <div>
      <p className="text-[11px] uppercase tracking-wide text-fg-subtle mb-1">
        {compact ? "Built from your answers" : "Your answers"}
      </p>
      <div className="flex flex-col">
        {shown.map((r) => {
          const { typed, attached } = typedPart(r.text);
          const isEditing = r.answer !== -1 && r.answer === editing;
          const after = editAt !== -1 && r.i > editAt;
          return (
            <div
              key={r.i}
              className={`border-b border-line py-2.5 ${after ? "opacity-40" : ""}`}
            >
              {!compact && (
                <p className="text-[10px] uppercase tracking-wide text-fg-subtle mb-0.5">
                  {r.role === "user" ? "You" : "Executive"}
                </p>
              )}
              {isEditing ? (
                <div className="flex flex-col gap-2">
                  <textarea
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    rows={3}
                    disabled={busy}
                    autoFocus
                    aria-label="Change your answer"
                    className="w-full rounded-lg border border-indigo-500/60 bg-surface-overlay px-3 py-2 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-indigo-500/50 resize-none disabled:opacity-50"
                  />
                  {attached && (
                    <p className="text-xs text-fg-muted">Your attached files are kept.</p>
                  )}
                  <p className="text-xs text-amber-400">
                    {compact
                      ? "Your profile is redrafted from here."
                      : "The questions after this one are asked again."}
                  </p>
                  {error && <p className="text-xs text-red-400">{error}</p>}
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => void save(r.answer)}
                      disabled={busy || !text.trim()}
                      className="px-3 py-1.5 bg-indigo-500 hover:bg-indigo-600 disabled:opacity-40 text-white text-xs font-medium rounded-lg transition-colors"
                    >
                      {busy ? "Thinking…" : "Save and continue"}
                    </button>
                    <button
                      onClick={() => {
                        setEditing(null);
                        setError(null);
                      }}
                      disabled={busy}
                      className="text-xs text-fg-muted hover:text-fg disabled:opacity-40 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex items-start gap-3">
                  <p
                    className={`flex-1 text-sm whitespace-pre-wrap ${
                      r.role === "user" ? "text-fg" : "text-fg-muted"
                    } ${compact ? "line-clamp-2" : ""}`}
                  >
                    {typed || (attached || r.answer === 0 ? "(attached files)" : "")}
                    {typed && attached && (
                      <span className="text-xs text-fg-subtle"> · with attached files</span>
                    )}
                  </p>
                  {r.role === "user" && (
                    <button
                      onClick={() => {
                        setEditing(r.answer);
                        setText(typed);
                        setError(null);
                      }}
                      disabled={disabled || busy || editing !== null}
                      className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-40 transition-colors"
                    >
                      Edit
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
