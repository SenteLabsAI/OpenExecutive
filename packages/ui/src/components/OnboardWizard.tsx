"use client";

import { useCallback, useEffect, useState } from "react";
import { startOnboarding, submitOnboardAnswer, type OnboardStatus } from "@/lib/api";

interface OnboardWizardProps {
  onComplete: () => void;
}

export default function OnboardWizard({ onComplete }: OnboardWizardProps) {
  const [status, setStatus] = useState<OnboardStatus | null>(null);
  const [answer, setAnswer] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const init = useCallback(async () => {
    setError(null);
    setAnswer("");
    setStatus(null);
    setIsLoading(true);
    try {
      const s = await startOnboarding();
      setStatus(s);
    } catch {
      setError("Failed to start onboarding. Is the API server running?");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    init();
  }, [init]);

  async function handleSubmit(skip = false) {
    if (!status) return;
    setIsLoading(true);
    setError(null);

    try {
      const next = await submitOnboardAnswer(
        status.session_id,
        skip ? "skip" : answer
      );
      setStatus(next);
      setAnswer("");

      if (next.completed) {
        setTimeout(onComplete, 1500);
      }
    } catch {
      setError("Failed to submit answer. Please try again.");
    } finally {
      setIsLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  if (error && !status) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center p-8 gap-4">
        <p className="text-red-400 text-sm">{error}</p>
        <button
          onClick={init}
          className="px-4 py-2 bg-indigo-500 hover:bg-indigo-600 text-white text-sm rounded-lg transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-6 h-6 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (status.completed) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center p-8 gap-4">
        <div className="w-14 h-14 bg-emerald-500/10 border border-emerald-500/20 rounded-full flex items-center justify-center">
          <svg className="w-7 h-7 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <div>
          <h2 className="text-lg font-semibold text-fg">Profile complete</h2>
          <p className="text-sm text-fg-muted mt-1">Redirecting you to the Executive...</p>
        </div>
      </div>
    );
  }

  const isOptionalStep = !status.current_step_required;

  return (
    <div className="flex flex-col h-full max-w-2xl mx-auto px-6 py-10">
      {/* Progress */}
      <div className="mb-10">
        <div className="flex justify-between text-xs text-fg-muted mb-2.5 font-medium">
          <span>Setting up your company profile</span>
          <span>{status.progress_percent}% complete</span>
        </div>
        <div className="w-full bg-surface-overlay rounded-full h-1.5">
          <div
            className="bg-indigo-500 h-1.5 rounded-full transition-all duration-500"
            style={{ width: `${status.progress_percent}%` }}
          />
        </div>
      </div>

      {/* Question */}
      <div className="flex-1 flex flex-col justify-center gap-8">
        <div className="gap-2 flex flex-col">
          <p className="text-xs font-semibold text-indigo-400 uppercase tracking-widest">
            Step {status.current_step + 1} of {status.total_steps}
            {isOptionalStep && (
              <span className="ml-2 text-fg-subtle normal-case font-normal tracking-normal">optional</span>
            )}
          </p>
          <h2 className="text-xl font-semibold text-fg leading-snug">
            {status.current_question}
          </h2>
        </div>

        {error && (
          <div className="flex items-center justify-between gap-3 text-red-400 text-sm bg-red-500/10 border border-red-500/20 rounded-lg px-4 py-3">
            <span>{error}</span>
            <button
              type="button"
              onClick={init}
              disabled={isLoading}
              className="shrink-0 text-xs font-medium text-fg-muted hover:text-fg disabled:opacity-50"
            >
              Restart onboarding
            </button>
          </div>
        )}

        <div className="flex flex-col gap-3">
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Type your answer..."
            rows={4}
            className="w-full rounded-xl border border-line-strong bg-surface-elevated px-4 py-3 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 resize-none transition-colors disabled:opacity-50"
            disabled={isLoading}
            autoFocus
          />

          <div className="flex gap-3">
            <button
              onClick={() => handleSubmit(false)}
              disabled={!answer.trim() || isLoading}
              className="flex-1 py-2.5 bg-indigo-500 hover:bg-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-xl transition-colors"
            >
              {isLoading ? "Saving..." : "Continue →"}
            </button>
            {isOptionalStep && (
              <button
                onClick={() => handleSubmit(true)}
                disabled={isLoading}
                className="px-5 py-2.5 border border-line-strong text-fg-muted hover:text-fg hover:border-line-strong text-sm rounded-xl transition-colors disabled:opacity-40"
              >
                Skip
              </button>
            )}
          </div>
          <p className="text-xs text-fg-muted text-center">
            Enter to continue · Shift+Enter for new line
          </p>
        </div>
      </div>
    </div>
  );
}
