/**
 * Example one-liners for describing a new workflow. Shared by the "Start
 * here" panel on /jobs and the conversational wizard at /jobs/new.
 */
export const WORKFLOW_STARTERS = [
  "A weekly competitor digest sent to me every Monday morning",
  "A board pre-read I kick off before each meeting, with the CFO signing off",
  "A monthly hiring-plan review across all open roles",
  "A launch readiness check I run before every product release",
];

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
