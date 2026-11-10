"use client";

import { useState } from "react";
import {
  answerUnansweredQuestion,
  dismissUnansweredQuestion,
  type UnansweredQuestion,
  type UnansweredQuestions as QuestionsPage,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import { askedLine } from "@/lib/unansweredQuestions";
import Button from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";

interface Props {
  questions: UnansweredQuestion[];
  /** The list after a change, as the server returns it. */
  onChange: (next: QuestionsPage) => void;
  /** Open the page's own add flow for this question. */
  onAddDocument: (id: number) => void;
  /** The question whose document is being added, if any. */
  addingFor: number | null;
}

// What people asked about the company that nothing here covered, at the top
// of Company documents. Not shown when there are none.
export default function UnansweredQuestions({ questions, onChange, onAddDocument, addingFor }: Props) {
  if (questions.length === 0) return null;
  return (
    <section className="space-y-3 max-w-5xl mb-10" aria-labelledby="unanswered-heading">
      <div>
        <h2 id="unanswered-heading" className="text-lg font-semibold text-fg">
          Questions I could not answer
          <span className="ml-2 font-medium text-fg-subtle tabular-nums">{questions.length}</span>
        </h2>
        <p className="mt-1 text-[15px] text-fg-muted">
          People asked these and nothing here covered them. Add a document, or tell me the answer
          and I will remember it.
        </p>
      </div>
      <ul className="divide-y divide-line rounded-2xl border border-line bg-surface-elevated">
        {questions.map((q) => (
          <QuestionRow
            key={q.id}
            question={q}
            onChange={onChange}
            onAddDocument={() => onAddDocument(q.id)}
            adding={addingFor === q.id}
          />
        ))}
      </ul>
    </section>
  );
}

function QuestionRow({
  question,
  onChange,
  onAddDocument,
  adding,
}: {
  question: UnansweredQuestion;
  onChange: (next: QuestionsPage) => void;
  onAddDocument: () => void;
  adding: boolean;
}) {
  const [answering, setAnswering] = useState(false);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<QuestionsPage>) {
    setBusy(true);
    setError(null);
    try {
      onChange(await action());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  }

  return (
    <li className="px-4 sm:px-5 py-4">
      <div className="flex flex-col lg:flex-row lg:items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-[15px] font-semibold text-fg break-words">{question.question}</p>
          <p className="mt-0.5 text-sm text-fg-muted">{askedLine(question, formatRelativeTime(question.last_asked_at))}</p>
        </div>
        {!answering && (
          <div className="flex flex-wrap items-center gap-2 flex-shrink-0">
            <Button size="sm" className="!h-10" onClick={onAddDocument} disabled={busy || adding}>
              <span className="inline-flex items-center gap-2">
                {adding && <Spinner />}
                Add a document
              </span>
            </Button>
            <Button size="sm" className="!h-10" onClick={() => setAnswering(true)} disabled={busy}>
              Tell me the answer
            </Button>
            <Button
              size="sm"
              className="!h-10 !text-fg-muted"
              onClick={() => run(() => dismissUnansweredQuestion(question.id))}
              disabled={busy}
            >
              Dismiss
            </Button>
          </div>
        )}
      </div>
      {answering && (
        <form
          className="mt-3 space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (answer.trim()) run(() => answerUnansweredQuestion(question.id, answer.trim()));
          }}
        >
          <label htmlFor={`answer-${question.id}`} className="sr-only">
            The answer
          </label>
          <textarea
            id={`answer-${question.id}`}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={3}
            maxLength={500}
            autoFocus
            placeholder="The answer, as you would tell a colleague"
            className="w-full rounded-xl border border-line bg-surface px-3 py-2 text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <div className="flex items-center gap-2">
            <Button type="submit" variant="primary" size="sm" className="!h-10" disabled={busy || !answer.trim()}>
              <span className="inline-flex items-center gap-2">
                {busy && <Spinner />}
                Remember it
              </span>
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="!h-10"
              onClick={() => {
                setAnswering(false);
                setError(null);
              }}
              disabled={busy}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </li>
  );
}
