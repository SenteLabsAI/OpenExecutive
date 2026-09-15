"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import OnboardWizard from "@/components/OnboardWizard";
import OnboardConversation, {
  type Bubble,
} from "@/components/onboard/OnboardConversation";
import OnboardDraftReview from "@/components/onboard/OnboardDraftReview";
import { type OnboardTurn } from "@/lib/api";

// Onboarding is a focused full-screen flow — exempt from the AppShell chrome
// (see AppShell.tsx EXEMPT_PREFIXES) so it owns the whole viewport.
//
// Default is the conversational flow: describe the business, answer a few
// clarifying questions, then review and edit a drafted profile. The original
// step-by-step wizard stays reachable at /onboard?mode=form — it needs no API
// key beyond the profile save, so it is also the fallback when the
// conversation cannot run.

function OnboardFlow() {
  const router = useRouter();
  const params = useSearchParams();
  const [turn, setTurn] = useState<OnboardTurn | null>(null);
  const [resumeTurns, setResumeTurns] = useState<Bubble[]>([]);
  const [conversationTurn, setConversationTurn] = useState<OnboardTurn | null>(null);

  function finish() {
    router.push("/");
  }

  if (params.get("mode") === "form") {
    return (
      <div className="max-w-3xl mx-auto w-full">
        <OnboardWizard onComplete={finish} />
        <p className="text-center text-xs text-fg-muted pb-10">
          <a href="/onboard" className="hover:text-fg transition-colors">
            ← Describe your business instead
          </a>
        </p>
      </div>
    );
  }

  if (turn?.phase === "draft") {
    return (
      <div className="max-w-3xl mx-auto px-6 py-10 w-full">
        <OnboardDraftReview
          turn={turn}
          onBackToConversation={() => {
            // Keep the session and its transcript — "ask me more" must not
            // throw away the interview.
            setConversationTurn({ ...turn, phase: "question", question: null });
            setTurn(null);
          }}
          onSaved={finish}
        />
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto px-6 py-16 w-full">
      <div className="mb-8">
        <h1 className="text-xl font-semibold text-fg">Set up your Executive</h1>
        <p className="text-sm text-fg-muted mt-1">
          A few minutes now, and every answer you get afterwards is grounded in
          your company rather than a generic one.
        </p>
      </div>

      <OnboardConversation
        initialTurn={conversationTurn}
        initialTurns={resumeTurns}
        onDraft={(next, bubbles) => {
          setResumeTurns(bubbles);
          setTurn(next);
        }}
      />

      <p className="text-center text-xs text-fg-subtle mt-10">
        <a
          href="/onboard?mode=form"
          className="hover:text-fg-muted transition-colors"
        >
          Prefer a form? Use the step-by-step version
        </a>
      </p>
    </div>
  );
}

export default function OnboardPage() {
  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        {/* useSearchParams needs a Suspense boundary for static prerender. */}
        <Suspense fallback={null}>
          <OnboardFlow />
        </Suspense>
      </main>
    </div>
  );
}
