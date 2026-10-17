"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import OnboardWizard from "@/components/OnboardWizard";
import OnboardConversation, {
  type Bubble,
} from "@/components/onboard/OnboardConversation";
import OnboardAnswers from "@/components/onboard/OnboardAnswers";
import OnboardDescribe from "@/components/onboard/OnboardDescribe";
import OnboardDone from "@/components/onboard/OnboardDone";
import OnboardDraftReview from "@/components/onboard/OnboardDraftReview";
import VoicePicker from "@/components/executive/VoicePicker";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  discardOnboardInterview,
  getOnboardInterview,
  type OnboardTurn,
  type WorkspaceMode,
} from "@/lib/api";
import { clearSetupMemory, recall, remember, STORAGE_KEYS } from "@/lib/firstRun";

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
//
// A setup in progress survives a refresh, a closed tab and a restart of the
// API: the interview is kept on the server, and this browser remembers which
// one is theirs (lib/firstRun.ts). Opening /onboard with one remembered
// picks it up at the same question or draft. After the save, a last screen
// shows what was kept and the two accounts worth connecting.

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
  const [showDone, setShowDone] = useState(false);
  // Resuming: the remembered session is being fetched.
  const [resuming, setResuming] = useState(() => recall(STORAGE_KEYS.session) !== null);
  const [resumed, setResumed] = useState(false);
  // Bumped by Start over, so the conversation starts again from empty.
  const [restarts, setRestarts] = useState(0);
  // Bumped by each redraft from an edited answer, so the review starts afresh.
  const [redrafts, setRedrafts] = useState(0);

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

  // Pick up the remembered session. Gone (saved, expired, another install):
  // forget it and start fresh.
  useEffect(() => {
    const sid = recall(STORAGE_KEYS.session);
    if (!sid) return;
    let cancelled = false;
    getOnboardInterview(sid)
      .then((session) => {
        if (cancelled) return;
        if (session.saved) {
          clearSetupMemory();
          return;
        }
        setResumeTurns(session.turns);
        if (session.phase === "draft") setTurn(session);
        else setConversationTurn(session);
        setResumed(true);
      })
      .catch(() => {
        if (!cancelled) remember(STORAGE_KEYS.session, null);
      })
      .finally(() => {
        if (!cancelled) setResuming(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Start over: forget this setup here and on the server, then begin again
  // from the description (or, on a re-run, from the opening question).
  function startOver() {
    const sid = recall(STORAGE_KEYS.session);
    if (sid) void discardOnboardInterview(sid).catch(() => {});
    clearSetupMemory();
    setTurn(null);
    setConversationTurn(null);
    setResumeTurns([]);
    setResumed(false);
    setChosenHere(null);
    setRestarts((n) => n + 1);
  }

  const resumedBanner = resumed && (
    <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-accent/25 bg-accent/5 px-4 py-3 text-sm text-fg-muted">
      <span>Picking up where you left off. Your answers were kept.</span>
      <button
        onClick={startOver}
        className="shrink-0 text-accent hover:opacity-80 transition-opacity"
      >
        Start over
      </button>
    </div>
  );

  function finish() {
    clearSetupMemory();
    // Only a first setup asks the voice; a re-run keeps what it has.
    if (chosenHere || resumed) setAskVoice(true);
    else router.push("/");
  }

  function later() {
    remember(STORAGE_KEYS.later, "1");
    remember(STORAGE_KEYS.description, null);
    router.push("/");
  }

  if (hasProfile === null || resuming) return null;

  if (showDone) return <OnboardDone solo={mode === "solo"} onDone={() => router.push("/")} />;

  if (askVoice) return <VoiceStep onDone={() => setShowDone(true)} />;

  if (!hasProfile && !chosenFor && !chosenHere && !resumed) {
    return (
      <OnboardDescribe
        onLater={later}
        onReady={({ mode: picked, turn: first, turns }) => {
          remember(STORAGE_KEYS.session, first.session_id);
          remember(STORAGE_KEYS.description, null);
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
        {resumedBanner}
        <OnboardDraftReview
          // A redraft after an edited answer starts the review afresh.
          key={redrafts}
          turn={turn}
          onBackToConversation={() => {
            // Keep the session and its transcript — "ask me more" must not
            // throw away the interview.
            setConversationTurn({ ...turn, phase: "question", question: null });
            setTurn(null);
          }}
          onSaved={finish}
          answers={
            <OnboardAnswers
              compact
              sessionId={turn.session_id}
              bubbles={resumeTurns}
              onRevised={(next, turns) => {
                setResumeTurns(turns);
                if (next.phase === "draft") {
                  setTurn(next);
                  setRedrafts((n) => n + 1);
                } else {
                  setConversationTurn(next);
                  setTurn(null);
                }
              }}
            />
          }
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

      {resumedBanner}

      <OnboardConversation
        key={restarts}
        initialTurn={conversationTurn}
        initialTurns={resumeTurns}
        solo={mode === "solo"}
        roleKind={role.role_kind}
        onDraft={(next, bubbles) => {
          remember(STORAGE_KEYS.session, next.session_id);
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
