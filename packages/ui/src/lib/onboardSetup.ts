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
