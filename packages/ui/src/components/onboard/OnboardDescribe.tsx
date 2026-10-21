"use client";

import { useEffect, useState } from "react";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { recall, remember, STORAGE_KEYS } from "@/lib/firstRun";
import { inferredMode, siteName, understoodRole, websiteNote } from "@/lib/onboardSetup";
import {
  startOnboardInterview,
  understandOnboarding,
  updateWorkspace,
  type OnboardTurn,
  type OnboardUnderstanding,
  type WorkspaceMode,
} from "@/lib/api";

// First run: your website (optional), then describe your work in one box (and
// optional files), then the interview starts. Nothing is asked in between.
// The server reads the website itself (onboarding/website.py), so it works
// with any model; a site it can't read just leaves the box to go on from.
//
// Continue reads the text once (POST /onboard/interview/understand) and saves
// what it can tell straight away: Personal or Team (Personal when it can't
// tell, since nothing is lost and switching is easy), the browser's time zone
// and, for Personal, the role the text gave. The review screen shows the
// choice on one line with a Change link, and Settings keeps the switch. Then
// the interview opens with the same text and asks only about what is missing.

const MAX_FILES = 8;

const EXAMPLE =
  "e.g. I'm Head of Customer Success at a 300-person software company and report to the COO. My team of twelve looks after renewals. This year I want net revenue retention above 110% and a cleaner handoff from sales.";

function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

export interface DescribeResult {
  mode: WorkspaceMode;
  turn: OnboardTurn;
  turns: { role: "user" | "assistant"; text: string }[];
  /** What was read from the website, or that it couldn't be; null without one. */
  websiteNote: string | null;
}

interface DescribeProps {
  onReady: (r: DescribeResult) => void;
  /** "I'll do this later": Home, with the checklist keeping setup one tap away. */
  onLater?: () => void;
}

export default function OnboardDescribe({ onReady, onLater }: DescribeProps) {
  const { refresh } = useWorkspace();
  // What was typed survives a refresh until Continue starts the interview.
  const [text, setText] = useState(() => recall(STORAGE_KEYS.description) ?? "");
  // Never guessed from the sign-in email: a work address's domain is often
  // not the company's site.
  const [website, setWebsite] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasInput = text.trim().length > 0 || files.length > 0 || website.trim().length > 0;
  const site = siteName(website);

  useEffect(() => {
    remember(STORAGE_KEYS.description, text.trim() ? text : null);
  }, [text]);

  async function saveWorkspace(u: OnboardUnderstanding | null): Promise<WorkspaceMode> {
    const mode = inferredMode(u);
    const timezone = browserTimeZone();
    try {
      await updateWorkspace(timezone ? { mode, timezone } : { mode });
    } catch (err) {
      // A zone the server does not know must not block setup.
      if (!timezone || (err instanceof Error && /principal/i.test(err.message))) throw err;
      await updateWorkspace({ mode });
    }
    const role = understoodRole(u);
    if (mode === "solo" && Object.keys(role).length > 0) {
      // Best effort: the interview asks about the role anyway.
      await updateWorkspace(role).catch(() => undefined);
    }
    await refresh();
    return mode;
  }

  async function begin(read: boolean) {
    if (busy || (read && !hasInput)) return;
    setBusy(true);
    setError(null);
    try {
      let understood: OnboardUnderstanding | null = null;
      if (read) {
        // Not fatal: without it setup starts Personal and the review says so.
        understood = await understandOnboarding(text.trim(), files, website).catch(() => null);
      }
      const mode = await saveWorkspace(understood);
      const description = read ? text.trim() : "";
      const turn = await startOnboardInterview(description, read ? files : [], read ? website : "");
      const pages = turn.website_pages ?? [];
      const turns: DescribeResult["turns"] = [];
      // Files or website pages alone still make the opening answer (the
      // server's first turn), so the answers line up with the server's when
      // one is edited.
      if (description || (read && (files.length > 0 || pages.length > 0))) {
        turns.push({ role: "user", text: description });
      }
      if (turn.phase === "question" && turn.question) {
        turns.push({ role: "assistant", text: turn.question });
      }
      onReady({ mode, turn, turns, websiteNote: read ? websiteNote(website, pages) : null });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start setup.");
      setBusy(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto px-4 sm:px-6 py-12 sm:py-16 w-full">
      <h1 className="text-xl font-semibold text-fg">Set up your Executive</h1>
      <p className="text-sm text-fg-muted mt-1">
        Tell me about your work in your own words. I&apos;ll work out the rest and ask a few
        quick questions about anything that&apos;s missing.
      </p>
      <label className="mt-6 block text-sm font-medium text-fg" htmlFor="onboard-website">
        Your website <span className="font-normal text-fg-muted">(optional)</span>
      </label>
      <input
        id="onboard-website"
        type="text"
        inputMode="url"
        autoComplete="url"
        value={website}
        onChange={(e) => setWebsite(e.target.value)}
        disabled={busy}
        autoFocus
        placeholder="example.com"
        className="mt-1.5 w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 transition-colors disabled:opacity-50"
      />
      {site && (
        <p className="mt-6 text-sm font-medium text-fg">
          Anything the website doesn&apos;t say?{" "}
          <span className="font-normal text-fg-muted">Your role, this year&apos;s goals, anything private.</span>
        </p>
      )}
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={7}
        disabled={busy}
        placeholder={EXAMPLE}
        aria-label={site ? "Anything the website doesn't say" : "Describe your work"}
        className={`${site ? "mt-2" : "mt-6"} w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 resize-none transition-colors disabled:opacity-50`}
      />
      <div className="mt-2 flex items-center gap-3">
        <label className="text-xs text-indigo-400 hover:text-indigo-300 cursor-pointer transition-colors">
          ＋ Attach a one-pager, deck or CV (optional)
          <input
            type="file"
            multiple
            className="hidden"
            accept=".pdf,.docx,.doc,.xlsx,.xlsm,.csv,.md,.txt"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, MAX_FILES))}
          />
        </label>
        {files.length > 0 && (
          <p className="text-xs text-fg-muted truncate">{files.map((f) => f.name).join(", ")}</p>
        )}
      </div>
      {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
      <div className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-2">
        <button
          type="button"
          onClick={() => void begin(true)}
          disabled={!hasInput || busy}
          className="px-5 py-2.5 rounded-lg text-sm font-medium bg-indigo-500 hover:bg-indigo-600 text-white transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? (site ? `Reading ${site}…` : "Reading…") : "Continue"}
        </button>
        <button
          type="button"
          onClick={() => void begin(false)}
          disabled={busy}
          className="text-sm text-fg-muted hover:text-fg transition-colors cursor-pointer disabled:opacity-50"
        >
          Skip, I&apos;ll answer questions instead
        </button>
      </div>
      {onLater && (
        <p className="mt-10 text-sm text-fg-subtle">
          Not now?{" "}
          <button
            type="button"
            onClick={onLater}
            disabled={busy}
            className="font-semibold text-accent hover:underline cursor-pointer disabled:opacity-50"
          >
            I&apos;ll do this later
          </button>
          . Home keeps setup one tap away.
        </p>
      )}
    </div>
  );
}
