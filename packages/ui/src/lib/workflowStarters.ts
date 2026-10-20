import type { WorkspaceMode } from "./api";

const COMPETITOR_DIGEST = "A weekly competitor digest sent to me every Monday morning";

/**
 * Example one-liners for describing a new workflow in a team workspace.
 * Shared by the "Start here" panel on /jobs and the conversational wizard at
 * /jobs/new (through `workflowStartersFor`).
 */
export const WORKFLOW_STARTERS = [
  COMPETITOR_DIGEST,
  "A board pre-read I kick off before each meeting, with the CFO signing off",
  "A monthly hiring-plan review across all open roles",
  "A launch readiness check I run before every product release",
];

/**
 * The same for someone running the business on their own ("Just you"): no
 * board, no CFO to sign off, no hiring plan, just the numbers they watch.
 */
export const SOLO_WORKFLOW_STARTERS = [
  "A Friday cash and pipeline check sent to me",
  "A monthly look at which customers stopped buying",
  COMPETITOR_DIGEST,
];

/**
 * Short chips for the front of /jobs in a team workspace: a label to tap and
 * the starter it puts in the box. The wizard shows the full sentences.
 */
export const WORKFLOW_STARTER_CHIPS: { label: string; text: string }[] = [
  { label: "Competitor digest", text: WORKFLOW_STARTERS[0] },
  { label: "Board pre-read", text: WORKFLOW_STARTERS[1] },
  { label: "Hiring plan review", text: WORKFLOW_STARTERS[2] },
];

export const SOLO_WORKFLOW_STARTER_CHIPS: { label: string; text: string }[] = [
  { label: "Friday cash check", text: SOLO_WORKFLOW_STARTERS[0] },
  { label: "Customers who stopped buying", text: SOLO_WORKFLOW_STARTERS[1] },
  { label: "Competitor digest", text: SOLO_WORKFLOW_STARTERS[2] },
];

/** The starter sentences for this workspace's mode. */
export function workflowStartersFor(mode: WorkspaceMode): string[] {
  return mode === "solo" ? SOLO_WORKFLOW_STARTERS : WORKFLOW_STARTERS;
}

/** The /jobs chips for this workspace's mode. */
export function workflowStarterChipsFor(mode: WorkspaceMode): { label: string; text: string }[] {
  return mode === "solo" ? SOLO_WORKFLOW_STARTER_CHIPS : WORKFLOW_STARTER_CHIPS;
}

export type StarterPick = { name: string; useFor: string };

/**
 * Ready-made workflows shown first in the ready-made panel on /jobs, each
 * with a plain line on when to use it. Names are WORKFLOW_REGISTRY keys; one
 * the catalog does not list is skipped.
 */
export const TEAM_STARTER_PICKS: StarterPick[] = [
  {
    name: "investor_update",
    useFor: "Send investors a short monthly email: highlights, numbers, asks.",
  },
  {
    name: "board_prep",
    useFor: "Get a full board deck drafted before your next meeting.",
  },
  {
    name: "mbr",
    useFor: "Review last month against plan and flag what needs a decision.",
  },
  {
    name: "competitive_teardown",
    useFor: "Size up a competitor and get battle-card material for sales.",
  },
];

/** For an owner-operator: no board or investors to report to. */
export const SOLO_STARTER_PICKS: StarterPick[] = [
  {
    name: "mbr",
    useFor: "Monthly review: last month against plan, and what needs a decision.",
  },
  {
    name: "pricing_review",
    useFor: "Check whether your prices still fit what customers pay for.",
  },
  {
    name: "churn_deep_dive",
    useFor: "Find out why customers left and what would have kept them.",
  },
  {
    name: "quarterly_plan",
    useFor: "Set the next quarter's few goals and the money behind them.",
  },
];

/** The first picks for this workspace's mode. */
export function starterPicksFor(mode: WorkspaceMode): StarterPick[] {
  return mode === "solo" ? SOLO_STARTER_PICKS : TEAM_STARTER_PICKS;
}

const HANDOFF_KEY = "oe.workflowWizard.describe";
// A hand-off older than this is from a Start the user walked away from, and
// must not fire on a later visit to the wizard.
const HANDOFF_TTL_MS = 5 * 60 * 1000;

/**
 * Hand a description typed on /jobs to the wizard, which sends it as the
 * first message. Session storage rather than the URL, so a link cannot make
 * the wizard send text the user never typed. Returns false when storage is
 * unavailable; the caller then falls back to `?describe=`, which only
 * prefills the composer.
 */
export function stashWorkflowDescription(text: string): boolean {
  try {
    // Cleared first, so a failed write can't leave an older hand-off behind.
    window.sessionStorage.removeItem(HANDOFF_KEY);
    window.sessionStorage.setItem(HANDOFF_KEY, JSON.stringify({ text, at: Date.now() }));
    return true;
  } catch {
    return false;
  }
}

/** Read and clear the handed-off description, so a refresh doesn't resend it. */
export function takeWorkflowDescription(): string | null {
  try {
    const raw = window.sessionStorage.getItem(HANDOFF_KEY);
    window.sessionStorage.removeItem(HANDOFF_KEY);
    if (!raw) return null;
    const { text, at } = JSON.parse(raw) as { text?: unknown; at?: unknown };
    const age = Date.now() - Number(at);
    return typeof text === "string" && age >= 0 && age < HANDOFF_TTL_MS ? text : null;
  } catch {
    return null;
  }
}
