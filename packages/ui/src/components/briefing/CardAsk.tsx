"use client";

// A Needs you card that asks something (ProposalItem.needs
// "answer" or "choice") is answered right on the card. The answer goes to
// POST /alerts/{id}/answer, and the Executive carries on with the work the
// card was for. Never Approve: the answer is the go.

import { useState } from "react";

import Button, { buttonClass } from "@/components/ui/Button";

const FIELD =
  "w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-[15px] leading-snug text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50";

export default function CardAsk({
  alertId,
  question,
  choices,
  onAnswer,
}: {
  alertId: number;
  question: string;
  choices: string[];
  onAnswer: (answer: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [typing, setTyping] = useState(choices.length === 0);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fieldId = `card-answer-${alertId}`;

  const send = async (answer: string) => {
    const value = answer.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onAnswer(value);
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't send your answer. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (done) return <p className="mt-3 text-sm text-fg-muted">Got it. I&apos;m on it.</p>;

  return (
    <div className="mt-3 space-y-2.5">
      {question && <p className="text-[15px] font-medium text-fg whitespace-pre-wrap break-words">{question}</p>}
      {choices.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Your answer">
          {choices.map((choice) => (
            <button
              key={choice}
              type="button"
              disabled={busy}
              onClick={() => void send(choice)}
              className={buttonClass("secondary", "md")}
            >
              {choice}
            </button>
          ))}
          {!typing && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setTyping(true)}
              className={buttonClass("ghost", "md")}
            >
              Something else…
            </button>
          )}
        </div>
      )}
      {typing && (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send(text);
          }}
        >
          <label htmlFor={fieldId} className="sr-only">
            Your answer
          </label>
          <textarea
            id={fieldId}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void send(text);
              }
            }}
            rows={3}
            disabled={busy}
            placeholder="Your answer"
            className={FIELD}
            autoFocus={choices.length > 0}
          />
          <Button type="submit" variant="primary" disabled={busy || !text.trim()}>
            {busy ? "Sending…" : "Send answer"}
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-500">
          {error}
        </p>
      )}
    </div>
  );
}
