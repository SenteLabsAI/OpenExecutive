"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import OnboardWizard from "@/components/OnboardWizard";
import OnboardConversation, {
  type Bubble,
} from "@/components/onboard/OnboardConversation";
import OnboardDraftReview from "@/components/onboard/OnboardDraftReview";
import RoleFields from "@/components/workspace/RoleFields";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { updateWorkspace, type OnboardTurn, type WorkspaceMode } from "@/lib/api";
import { roleFormErrors, roleFormFrom, roleUpdate, type RoleForm } from "@/lib/principalRole";

// Onboarding is a focused full-screen flow — exempt from the AppShell chrome
// (see AppShell.tsx EXEMPT_PREFIXES) so it owns the whole viewport.
//
// First run starts with one question: is this just for you, or for you and
// your team? The answer is saved as the workspace mode (PUT /workspace, with
// the browser's time zone) before anything else, because the conversation
// and the form ask different things in each mode. "Just me" then asks what
// your role is — owner, an executive inside an organisation, independent —
// saved with the workspace settings too (skippable; editable in Settings),
// so setup asks the right questions and the Executive advises for that role.
// A re-run — a company profile already exists — skips both: they are changed
// in Settings, and the flow below follows whatever they currently are.
//
// Then the conversational flow: describe the business, answer a few
// clarifying questions, then review and edit a drafted profile. The original
// step-by-step wizard stays reachable at /onboard?mode=form — it needs no API
// key beyond the profile save, so it is also the fallback when the
// conversation cannot run.
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
    router.push("/");
  }

  if (hasProfile === null) return null;

  if (!hasProfile && !chosenFor) {
    return (
      <WorkspaceChoice
        onChosen={(chosen) => router.replace(onboardHref(formMode, FOR_PARAM[chosen]))}
      />
    );
  }

  // Carried on the links between the conversation and the form.
  const forParam = chosenFor ?? FOR_PARAM[mode];

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

// Solo is for anyone using Open Executive just for themselves, whatever
// their role — not only someone running a business. The two bodies are kept
// the same shape and about the same length, so they read as a pair.
const CHOICES: { mode: WorkspaceMode; title: string; body: string }[] = [
  {
    mode: "solo",
    title: "Just me",
    body: "An executive that works for you, whatever your role: your own business, a team you lead, or clients you advise. It keeps track of your goals and promises, briefs you each morning and reviews your week with you. The people you work with stay your private contacts.",
  },
  {
    mode: "team",
    title: "Me and my team",
    body: "An executive that works for you and your team. It organises your company into departments, each with its own goals and a lead, checks in with every department daily and briefs you each morning. Your teammates can sign in and message it too.",
  },
];

// The browser's IANA zone, or undefined when it cannot say.
function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

function WorkspaceChoice({ onChosen }: { onChosen: (mode: WorkspaceMode) => void }) {
  const { refresh } = useWorkspace();
  const [saving, setSaving] = useState<WorkspaceMode | null>(null);
  const [error, setError] = useState<string | null>(null);
  // "Just me" was saved: ask the role before moving on.
  const [askRole, setAskRole] = useState(false);

  async function choose(mode: WorkspaceMode) {
    if (saving) return;
    setSaving(mode);
    setError(null);
    try {
      const timezone = browserTimeZone();
      try {
        await updateWorkspace(timezone ? { mode, timezone } : { mode });
      } catch (err) {
        // A zone the server does not know must not block setup: save the
        // mode alone and leave the zone to Settings.
        if (!timezone || (err instanceof Error && /principal/i.test(err.message))) throw err;
        await updateWorkspace({ mode });
      }
      await refresh();
      if (mode === "solo") {
        setAskRole(true);
        setSaving(null);
        return;
      }
      onChosen(mode);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save your choice.");
      setSaving(null);
    }
  }

  if (askRole) return <RoleStep onDone={() => onChosen("solo")} />;

  return (
    <div className="max-w-2xl mx-auto px-6 py-16 w-full">
      <h1 className="text-xl font-semibold text-fg">Who is Open Executive for?</h1>
      <p className="text-sm text-fg-muted mt-1">
        Your answer decides what setup asks and what you see afterwards. You can change it
        later in Settings.
      </p>

      <div className="mt-8 grid gap-3 sm:grid-cols-2">
        {CHOICES.map((c) => (
          <button
            key={c.mode}
            type="button"
            onClick={() => void choose(c.mode)}
            disabled={saving !== null}
            className="flex flex-col justify-start text-left rounded-xl border border-line bg-surface-elevated p-5 hover:border-indigo-500/60 hover:bg-surface-overlay transition-colors cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-indigo-500/50"
          >
            <span className="block text-sm font-semibold text-fg">
              {saving === c.mode ? "Saving…" : c.title}
            </span>
            <span className="block text-xs text-fg-muted mt-1.5 leading-relaxed">{c.body}</span>
          </button>
        ))}
      </div>

      {error && (
        <div className="mt-4 text-xs">
          <p className="text-red-400">{error}</p>
          <button
            type="button"
            onClick={() => onChosen("team")}
            className="mt-1 text-fg-muted hover:text-fg underline underline-offset-2 cursor-pointer"
          >
            Continue without saving a choice
          </button>
        </div>
      )}
    </div>
  );
}

// "Just me" → what is your role? Saved with the workspace settings; skipping
// saves nothing, and Settings → Workspace edits it later.
function RoleStep({ onDone }: { onDone: () => void }) {
  const { role, refresh } = useWorkspace();
  // Mounted right after the mode was saved and re-read, so `role` is current.
  const [form, setForm] = useState<RoleForm>(() => roleFormFrom(role));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const problems = roleFormErrors(form);

  async function save() {
    if (saving || problems.length > 0) return;
    const update = roleUpdate(form, role);
    if (Object.keys(update).length === 0) {
      onDone();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await updateWorkspace(update);
      await refresh();
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save your role.");
      setSaving(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto px-6 py-16 w-full">
      <h1 className="text-xl font-semibold text-fg">What&apos;s your role?</h1>
      <p className="text-sm text-fg-muted mt-1">
        So setup asks the right questions and the advice fits your job — a case to put to your
        boss, or a call that&apos;s yours alone to make. You can change it later in Settings.
      </p>

      <div className="mt-8 rounded-xl border border-line bg-surface-elevated p-5">
        <RoleFields value={form} onChange={setForm} disabled={saving} idPrefix="onboard-role" />
      </div>

      {(problems.length > 0 || error) && (
        <div className="mt-3 space-y-1 text-xs text-red-400">
          {problems.map((p) => (
            <p key={p}>{p}</p>
          ))}
          {error && <p>{error}</p>}
        </div>
      )}

      <div className="mt-6 flex items-center gap-3">
        <button
          type="button"
          onClick={() => void save()}
          disabled={saving || problems.length > 0}
          className="px-4 py-2 rounded-lg text-sm font-medium bg-indigo-500 hover:bg-indigo-600 text-white transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? "Saving…" : "Continue"}
        </button>
        <button
          type="button"
          onClick={onDone}
          disabled={saving}
          className="px-3 py-2 rounded-lg text-sm text-fg-muted hover:text-fg transition-colors cursor-pointer disabled:opacity-50"
        >
          Skip for now
        </button>
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
