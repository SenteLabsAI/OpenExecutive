"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import OnboardWizard from "@/components/OnboardWizard";
import OnboardConversation, {
  type Bubble,
} from "@/components/onboard/OnboardConversation";
import OnboardDescribe from "@/components/onboard/OnboardDescribe";
import OnboardDraftReview from "@/components/onboard/OnboardDraftReview";
import VoicePicker from "@/components/executive/VoicePicker";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import type { OnboardTurn, WorkspaceMode } from "@/lib/api";

// Onboarding is a focused full-screen flow — exempt from the AppShell chrome
// (see AppShell.tsx EXEMPT_PREFIXES) so it owns the whole viewport.
//
// First run starts with the free-text box: describe your work (and attach a
// one-pager if you like). The Executive reads it, shows what it understood —
// personal or team, role, company, focus — for a quick confirm, saves the
// workspace mode and role, and then asks only about what is still missing
// (up to five questions, each skippable). Then the drafted profile is
// reviewed and, last, how the Executive should sound (skippable; changeable
// in Settings). A re-run — a company profile already exists — skips the
// first two: they are changed in Settings.
//
// The original step-by-step wizard stays reachable at /onboard?mode=form — it
// needs no API key beyond the profile save, so it is also the fallback when
// the conversation cannot run.
//
// `?for=me|team` records that the choice was made, so moving between the
// conversation and the form does not ask again.

const FOR_PARAM: Record<WorkspaceMode, string> = { solo: "me", team: "team" };

function onboardHref(form: boolean, chosenFor: string | null): string {
  const q = new URLSearchParams();
  if (form) q.set("mode", "form");
  if (chosenFor) q.set("for", chosenFor);
  const qs = q.toString();
  return qs ? `/onboard?${qs}` : "/onboard";
}

function OnboardFlow() {
  const router = useRouter();
  const params = useSearchParams();
  const { mode, role } = useWorkspace();
  const [turn, setTurn] = useState<OnboardTurn | null>(null);
  const [resumeTurns, setResumeTurns] = useState<Bubble[]>([]);
  const [conversationTurn, setConversationTurn] = useState<OnboardTurn | null>(null);
  // null while we find out whether a profile exists (a re-run skips the choice).
  const [hasProfile, setHasProfile] = useState<boolean | null>(null);
  // Set once the describe-first step is done, in place of the `?for=` param.
  const [chosenHere, setChosenHere] = useState<WorkspaceMode | null>(null);
  const [askVoice, setAskVoice] = useState(false);

  const formMode = params.get("mode") === "form";
  const chosenFor = params.get("for");

  useEffect(() => {
    let cancelled = false;
    fetch("/api/backend/health")
      .then((r) => r.json())
      .then((h: { company_profile_loaded?: boolean }) => {
        if (!cancelled) setHasProfile(h.company_profile_loaded === true);
      })
      // Unknown: ask. Answering the choice again is harmless.
      .catch(() => {
        if (!cancelled) setHasProfile(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function finish() {
    // Only a first setup asks the voice; a re-run keeps what it has.
    if (chosenHere) setAskVoice(true);
    else router.push("/");
  }

  if (hasProfile === null) return null;

  if (askVoice) return <VoiceStep onDone={() => router.push("/")} />;

  if (!hasProfile && !chosenFor && !chosenHere) {
    return (
      <OnboardDescribe
        onReady={({ mode: picked, turn: first, turns }) => {
          setResumeTurns(turns);
          if (first.phase === "draft") setTurn(first);
          else setConversationTurn(first);
          setChosenHere(picked);
        }}
      />
    );
  }

  // Carried on the links between the conversation and the form.
  const forParam = chosenFor ?? (chosenHere ? FOR_PARAM[chosenHere] : FOR_PARAM[mode]);

  if (formMode) {
    return (
      <div className="max-w-3xl mx-auto w-full">
        <OnboardWizard onComplete={finish} />
        <p className="text-center text-xs text-fg-muted pb-10">
          <a href={onboardHref(false, forParam)} className="hover:text-fg transition-colors">
            ← Describe your {mode === "solo" ? "work" : "business"} instead
          </a>
        </p>
      </div>
    );
  }

  if (turn?.phase === "draft") {
    return (
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-10 w-full">
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
    <div className="max-w-2xl mx-auto px-4 sm:px-6 py-16 w-full">
      <div className="mb-8">
        <h1 className="text-xl font-semibold text-fg">Set up your Executive</h1>
        <p className="text-sm text-fg-muted mt-1">
          A few minutes now, and every answer you get afterwards is grounded in
          your {mode === "solo" ? "work" : "company"} rather than a generic one.
        </p>
      </div>

      <OnboardConversation
        initialTurn={conversationTurn}
        initialTurns={resumeTurns}
        solo={mode === "solo"}
        roleKind={role.role_kind}
        onDraft={(next, bubbles) => {
          setResumeTurns(bubbles);
          setTurn(next);
        }}
      />

      <p className="text-center text-xs text-fg-subtle mt-10">
        <a
          href={onboardHref(true, forParam)}
          className="hover:text-fg-muted transition-colors"
        >
          Prefer a form? Use the step-by-step version
        </a>
      </p>
    </div>
  );
}

// How should the Executive sound? Saved on the Executive (PATCH
// /agents/executive); skipping keeps Direct. Settings → Executive changes it.
function VoiceStep({ onDone }: { onDone: () => void }) {
  return (
    <div className="max-w-3xl mx-auto px-4 sm:px-6 py-16 w-full">
      <h1 className="text-xl font-semibold text-fg">How should your Executive sound?</h1>
      <p className="text-sm text-fg-muted mt-1">
        The advice is the same; this sets the tone it comes in. You can change it later in
        Settings.
      </p>
      <div className="mt-8">
        <VoicePicker variant="step" onDone={onDone} />
      </div>
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
