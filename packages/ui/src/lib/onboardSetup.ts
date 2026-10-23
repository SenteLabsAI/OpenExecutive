// Pure helpers for the setup flow (/onboard), kept out of the components so
// scripts/onboardSetup.test.mjs can check them under
// `node --experimental-strip-types` (type-only imports).

import type {
  CompanyProfile,
  OnboardGoalDraft,
  OnboardUnderstanding,
  WorkspaceMode,
  WorkspaceUpdate,
} from "@/lib/api";

/** The mode setup saves without asking: what the text says, else Personal
 * (nothing is lost, and adding a teammate later switches to Team). */
export function inferredMode(u: OnboardUnderstanding | null): WorkspaceMode {
  return u?.mode ?? "solo";
}

/** The role fields the text gave, for a Personal workspace. Only what was
 * said: a field the text left out keeps whatever is already saved. */
export function understoodRole(u: OnboardUnderstanding | null): WorkspaceUpdate {
  const out: WorkspaceUpdate = {};
  if (u?.role_kind) out.role_kind = u.role_kind;
  if (u?.role_title) out.role_title = u.role_title;
  if (u?.reports_to) out.reports_to = u.reports_to;
  return out;
}

const SHOWN_NAMES = 4;

/** The one line the review shows in place of a "Who is this for?" question. */
export function modeLine(mode: WorkspaceMode, names: string[]): string {
  if (mode === "solo") return "Set up just for you";
  const named = names.map((n) => n.trim()).filter(Boolean);
  if (named.length === 0) return "Set up for your team";
  const shown = named.slice(0, SHOWN_NAMES);
  const rest = named.length - shown.length;
  return `Set up for your team: ${shown.join(", ")}${rest > 0 ? ` and ${rest} more` : ""}`;
}

// A snake_case word ("annual_revenue_arr") is a field name, not a sentence.
const FIELD_NAME = /\b[a-z]+(?:_[a-z0-9]+)+\b/;

/** The "Please check" lines: the drafter's notes, minus any that still
 * speak in field names (the prompt asks for plain sentences; this is the
 * backstop) and blanks. */
export function checkNotes(notes: string[]): string[] {
  return notes.map((n) => n.trim()).filter((n) => n && !FIELD_NAME.test(n));
}

/** What happens after Save, as the review lists it under "After you save,
 * I'll". Research always runs once setup is saved
 * (api/routes/onboarding.py, _fire_post_onboarding_research). */
export function afterSaveSteps(profile: CompanyProfile, goals: OnboardGoalDraft[]): string[] {
  const steps: string[] = [];
  const competitors = profile.competitive_landscape?.primary_competitors ?? [];
  steps.push(
    competitors.length > 0
      ? "Research your market and competitors, and put what's worth knowing in your brief"
      : "Research your competitors and suggest a shortlist in your brief",
  );
  if (goals.some((g) => g.title.trim())) {
    steps.push("Check your goals every week and flag any that are at risk in your brief");
  }
  const watched = [...(profile.vendors ?? []), ...(profile.tickers ?? [])].filter((v) => v.trim());
  if (watched.length > 0) {
    steps.push(`Watch the status pages and news for ${watched.join(", ")}`);
  }
  return steps;
}

/** Goals as the commit sends them: named ones only, trimmed, and any owner
 * or department that no longer exists on the review screen cleared (the
 * server would refuse the whole save over it). */
export function goalsToSave(
  goals: OnboardGoalDraft[],
  people: string[],
  departments: string[],
): OnboardGoalDraft[] {
  const names = new Set(people.map((p) => p.trim().toLowerCase()).filter(Boolean));
  const titles = new Set(departments.map((d) => d.trim().toLowerCase()).filter(Boolean));
  return goals
    .filter((g) => g.title.trim())
    .map((g) => ({
      title: g.title.trim(),
      target: g.target.trim(),
      owner_name: names.has(g.owner_name.trim().toLowerCase()) ? g.owner_name.trim() : "",
      department_title: titles.has(g.department_title.trim().toLowerCase())
        ? g.department_title.trim()
        : "",
    }));
}

/** What was typed in the website field, as people say it: "example.com". */
export function siteName(typed: string): string {
  return typed
    .trim()
    .replace(/^[a-z][a-z0-9+.-]*:\/\//i, "")
    .replace(/^www\./i, "")
    .replace(/[/?#].*$/, "")
    .toLowerCase();
}

/** A page label from the server ("example.com/about-us") as a name: "About us",
 * or "Home" for the site itself. */
export function pageName(label: string): string {
  const slash = label.indexOf("/");
  if (slash === -1) return "Home";
  const last = label.slice(slash + 1).split("/").filter(Boolean).pop() ?? "";
  const words = last.replace(/[-_]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Home";
}

/** The line the questions open with when a website was given: what was read,
 * or that it couldn't be (setup then goes on from what was written). */
export function websiteNote(typed: string, pages: string[]): string | null {
  const site = siteName(typed);
  if (!site) return null;
  if (pages.length === 0) return `I couldn't read ${site}, so I'll ask instead.`;
  return `Read ${site}: ${pages.map(pageName).join(", ")}.`;
}

/** Where Continue has got to: reading what was given (the understand call,
 * which also reads the website), then opening the questions (the interview's
 * first turn). */
export type SetupStage = "reading" | "starting";

export interface SetupStep {
  label: string;
  state: "done" | "active" | "waiting";
}

/** The steps shown under Continue while it works, so a dimmed button is never
 * all there is to see. Skip has nothing to read, so it shows only the last. */
export function setupSteps(
  stage: SetupStage,
  opts: { read: boolean; site: string; wrote: boolean; files: number },
): SetupStep[] {
  const labels: string[] = [];
  if (opts.read) {
    // One step, not two: the server reads the site and then the model reads
    // it all in one call, so there is no moment to tick the site off.
    const sources = [opts.site, opts.wrote ? "what you wrote" : "", opts.files > 0 ? "your files" : ""].filter(
      Boolean,
    );
    const read =
      sources.length > 1 ? `${sources.slice(0, -1).join(", ")} and ${sources[sources.length - 1]}` : (sources[0] ?? "what you gave");
    labels.push(opts.site ? `Reading ${read}: what you do and who's on the team` : `Reading ${read}`);
  }
  labels.push("Getting your first question ready");
  const active = stage === "starting" ? labels.length - 1 : 0;
  return labels.map((label, i) => ({
    label,
    state: i < active ? "done" : i === active ? "active" : "waiting",
  }));
}
