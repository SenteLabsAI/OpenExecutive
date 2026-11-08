// The Approvals level (Careful / Balanced / Hands-off) in a few words, for
// the sidebar's quick picker. Settings → Your Executive keeps the full card
// with examples (components/settings/OutsideContentCard.tsx); both save the
// same setting (/executive/outside-content, orchestrator/levels.py).

export type ApprovalsLevel = "ask" | "outsiders" | "trust";

export const APPROVALS_LEVELS: { value: ApprovalsLevel; title: string; text: string }[] = [
  { value: "ask", title: "Careful", text: "I ask before I send, book, save or run anything." },
  {
    value: "outsiders",
    title: "Balanced",
    text: "I go ahead with your team and People. I ask before anyone else, or when something I read tries to give me orders.",
  },
  { value: "trust", title: "Hands-off", text: "I just do it. I don't stop to ask." },
];

// Sent on window when either picker saves, carrying the saved setting, so
// the other shows the new level without loading it again.
export const APPROVALS_CHANGED_EVENT = "oe:approvals-changed";

export function announceApprovals<T>(saved: T): void {
  window.dispatchEvent(new CustomEvent<T>(APPROVALS_CHANGED_EVENT, { detail: saved }));
}

// Calls `fn` with each saved setting; returns the unsubscribe.
export function onApprovalsChanged<T>(fn: (saved: T) => void): () => void {
  const listener = (e: Event) => fn((e as CustomEvent<T>).detail);
  window.addEventListener(APPROVALS_CHANGED_EVENT, listener);
  return () => window.removeEventListener(APPROVALS_CHANGED_EVENT, listener);
}

export function approvalsTitle(level: string): string {
  return APPROVALS_LEVELS.find((l) => l.value === level)?.title ?? "Careful";
}
