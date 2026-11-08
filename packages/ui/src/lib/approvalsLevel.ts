// The Approvals level (Careful / Balanced / Hands-off) in a few words, for
// the sidebar's quick picker. Settings → Your Executive keeps the full card
// with examples (components/settings/OutsideContentCard.tsx); both save the
// same setting (/executive/outside-content, orchestrator/levels.py).

export type ApprovalsLevel = "ask" | "outsiders" | "trust";

export const APPROVALS_LEVELS: { value: ApprovalsLevel; title: string; text: string }[] = [
  { value: "ask", title: "Careful", text: "I ask before I send, book, save or run anything." },
  { value: "outsiders", title: "Balanced", text: "I go ahead with your team and People. I ask before anyone else." },
  { value: "trust", title: "Hands-off", text: "I just do it. I don't stop to ask." },
];

// Sent on window when either picker saves, so the other shows the new level.
export const APPROVALS_CHANGED_EVENT = "oe:approvals-changed";

export function approvalsTitle(level: string): string {
  return APPROVALS_LEVELS.find((l) => l.value === level)?.title ?? "Careful";
}

// Take the lead in training holds at every level: what it starts on its own
// waits for a yes unless it was allowed. Said under the levels while it's on.
export function trainingNote(lead: { enabled: boolean; training: boolean } | null): string | null {
  if (!lead || !lead.enabled || !lead.training) return null;
  return "What I start on my own still waits for you.";
}
