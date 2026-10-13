// The first run, as pure rules: when Home sends the owner to setup, what the
// browser remembers between page loads, and what the getting-started
// checklist shows. The components in components/onboard/ read these; the
// tests in scripts/firstRun.test.mjs pin them.

import type { SetupCheck } from "@/lib/setupStatus";

// Browser-side memory of a setup in progress. The interview itself is kept
// on the server (GET /onboard/interview/{id}); the browser only keeps which
// one is theirs, the description typed before Continue, and that they chose
// to do setup later.
export const STORAGE_KEYS = {
  session: "oe.onboard.session",
  description: "oe.onboard.description",
  later: "oe.onboard.later",
  checklistHidden: "oe.gettingStarted.hidden",
} as const;

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function remember(key: string, value: string | null): void {
  const s = storage();
  if (!s) return;
  try {
    if (value === null) s.removeItem(key);
    else s.setItem(key, value);
  } catch {
    // Private mode or a full store: setup still works, it just won't resume.
  }
}

export function recall(key: string): string | null {
  const s = storage();
  if (!s) return null;
  try {
    return s.getItem(key);
  } catch {
    return null;
  }
}

/** Forget everything about the setup in progress (after it is saved). */
export function clearSetupMemory(): void {
  remember(STORAGE_KEYS.session, null);
  remember(STORAGE_KEYS.description, null);
  remember(STORAGE_KEYS.later, null);
}

export interface RedirectInput {
  /** `company_profile_loaded` from /health; undefined while unknown. */
  profileLoaded: boolean | undefined;
  /** The viewer is the owner (the person setup is for); undefined while unknown. */
  isPrincipal: boolean | undefined;
  /** They chose "I'll do this later" on the setup screen. */
  later: boolean;
}

/**
 * Whether a page should send the viewer to /onboard. Only once both answers
 * are in, only for the owner (a teammate can't finish setup for them), and
 * not after they asked to do it later: the checklist on Home keeps the way
 * back.
 */
export function shouldRedirectToOnboard(input: RedirectInput): boolean {
  if (input.profileLoaded === undefined || input.isPrincipal === undefined) return false;
  return !input.profileLoaded && input.isPrincipal && !input.later;
}

// Where each checklist item sends people. One place, so a deployment with
// its own connection screens points these elsewhere.
export const FIRST_RUN_LINKS = {
  setup: "/onboard",
  execMailbox: "/settings/status",
  ownMailbox: "/settings/act-as-me",
  people: "/people",
} as const;

export type ChecklistItemId = "profile" | "ask" | "exec_mailbox" | "own_mailbox" | "team";

export interface ChecklistItem {
  id: ChecklistItemId;
  title: string;
  detail: string;
  done: boolean;
  optional: boolean;
  href: string;
  action: string;
}

export interface ChecklistInput {
  profileLoaded: boolean;
  companyName: string | null | undefined;
  /** Any conversation exists. */
  hasAsked: boolean;
  /** /setup/status checks, or null while loading or when it failed. */
  checks: readonly SetupCheck[] | null;
  /** Team mode: the roster has more than the owner. */
  team: boolean;
  teamSize: number;
}

function checkState(checks: readonly SetupCheck[] | null, id: string): SetupCheck["state"] | null {
  const c = checks?.find((x) => x.id === id);
  return c ? c.state : null;
}

/** The checklist rows, in the order they are shown. */
export function gettingStartedItems(input: ChecklistInput): ChecklistItem[] {
  const execMail = checkState(input.checks, "gmail");
  const ownMail = checkState(input.checks, "your_gmail");
  const items: ChecklistItem[] = [
    {
      id: "profile",
      title: "Profile set up",
      detail: input.profileLoaded
        ? `${input.companyName?.trim() || "Saved"} · you are the owner`
        : "A few lines about your work; the Executive fills in the rest.",
      done: input.profileLoaded,
      optional: false,
      href: FIRST_RUN_LINKS.setup,
      action: "Set up",
    },
    {
      id: "ask",
      title: "Ask your Executive something",
      detail: input.hasAsked ? "First conversation saved" : "Try “What should I focus on this week?”",
      done: input.hasAsked,
      optional: false,
      href: "/?new=1",
      action: "Ask",
    },
    {
      id: "exec_mailbox",
      title: "Give your Executive its own email",
      detail: "A Google or Microsoft account just for it. Powers the morning brief.",
      done: execMail === "ok",
      optional: false,
      href: FIRST_RUN_LINKS.execMailbox,
      action: "Connect",
    },
    {
      id: "own_mailbox",
      title: "Let it read your own mailbox",
      detail: "Optional. Drafts replies as you, never sends.",
      done: ownMail === "ok",
      optional: true,
      href: FIRST_RUN_LINKS.ownMailbox,
      action: "Connect",
    },
  ];
  if (input.team) {
    items.push({
      id: "team",
      title: "Add your team",
      detail:
        input.teamSize > 1
          ? `${input.teamSize} people on the People page`
          : "Who the Executive works with, and who reports to whom.",
      done: input.teamSize > 1,
      optional: false,
      href: FIRST_RUN_LINKS.people,
      action: "Add",
    });
  }
  return items;
}

/** "2 of 5" for the heading; optional rows count once done. */
export function checklistProgress(items: readonly ChecklistItem[]): { done: number; total: number } {
  const required = items.filter((i) => !i.optional);
  return { done: required.filter((i) => i.done).length, total: required.length };
}

/** The checklist leaves Home once every required row is done. */
export function checklistComplete(items: readonly ChecklistItem[]): boolean {
  return items.every((i) => i.optional || i.done);
}
