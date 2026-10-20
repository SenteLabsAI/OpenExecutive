// A Today card approved with POST /alerts/{id}/approve runs on the server
// (alerts/approvals.py). The briefing keeps a row per approval made on this
// page and polls it until the work has finished. These helpers decide what
// that row says; ApprovedRow renders it.

export type ApprovalStatus = "running" | "done" | "failed";

export interface AlertApproval {
  id: number;
  alert_id: number;
  approved_by_person_id: number | null;
  approved_at: string;
  edited_body: string | null;
  status: ApprovalStatus;
  outcome: string;
  session_id: string | null;
  finished_at: string | null;
}

// How often a running approval is re-read, and for how long. A turn that
// runs longer still finishes on the server; the row just stops checking.
export const APPROVAL_POLL_MS = 4000;
export const APPROVAL_POLL_MAX_MS = 30 * 60 * 1000;

export interface ApprovalLine {
  tone: "muted" | "emerald" | "rose";
  text: string;
  // Where the work can be read, when it ran as a chat.
  chatHref: string | null;
  chatLabel: string | null;
  canRetry: boolean;
}

export function chatHref(sessionId: string | null): string | null {
  return sessionId ? `/?session=${encodeURIComponent(sessionId)}` : null;
}

export function approvalLine(approval: AlertApproval, headline: string): ApprovalLine {
  const href = chatHref(approval.session_id);
  if (approval.status === "running") {
    return {
      tone: "muted",
      text: `Working on it: ${headline}`,
      chatHref: href,
      chatLabel: href ? "Watch it" : null,
      canRetry: false,
    };
  }
  if (approval.status === "done") {
    return {
      tone: "emerald",
      text: href ? `Done: ${headline}` : `Done: ${headline}. ${approval.outcome}`.trim(),
      chatHref: href,
      chatLabel: href ? "See what it did" : null,
      canRetry: false,
    };
  }
  return {
    tone: "rose",
    text: `Didn't work: ${headline}${approval.outcome ? `. ${approval.outcome}` : ""}`,
    chatHref: href,
    chatLabel: href ? "Open the chat" : null,
    canRetry: true,
  };
}
