// Always in the loop: how the History tab and the settings word a person's
// notes and how long they last, from GET /memories/history. Kept free of
// React so scripts/ can test it.

export interface HistoryNote {
  id: number;
  source: string;
  channel: string;
  conversation_key: string;
  counterpart: string;
  subject: string;
  kind: string;
  summary: string;
  quote: string;
  due_date: string | null;
  trust: string;
  occurred_at: string;
  created_at: string;
  expires_at: string | null;
  pinned: boolean;
  correction: string | null;
  corrected_at: string | null;
}

export interface HistoryState {
  notes: HistoryNote[];
  reply_notes: boolean;
  /** The person's own choice; null is the company default. */
  retention_days: number | null;
  /** What applies to their new notes; null is until they forget them. */
  effective_retention_days: number | null;
  /** null is until forgotten. */
  company_retention_days: number | null;
  retention_choices: (number | null)[];
  can_note_replies: boolean;
  can_set_company_retention: boolean;
}

/** "30 days", "1 year", or "until forgotten" for null. */
export function retentionLabel(days: number | null): string {
  if (days === null) return "until forgotten";
  if (days === 365) return "1 year";
  return `${days} ${days === 1 ? "day" : "days"}`;
}

/** The choices a person may pick for their own notes: the company default
 * (null), then each choice shorter than the company's. Their stored choice
 * is always among them, even one no longer shorter (the owner shortening the
 * company time clamps it to the company's), so the box never shows a value
 * they didn't pick. */
export function personRetentionChoices(
  choices: (number | null)[],
  company: number | null,
  current: number | null = null,
): (number | null)[] {
  const shorter = choices.filter(
    (d): d is number => d !== null && (company === null || d < company),
  );
  if (current !== null && !shorter.includes(current)) shorter.push(current);
  return [null, ...shorter.sort((a, b) => a - b)];
}

const KIND_LABELS: Record<string, string> = {
  promised: "Promised",
  agreed: "Agreed",
  declined: "Declined",
  answered: "Answered",
  asked: "Asked",
  shared: "Shared",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind.charAt(0).toUpperCase() + kind.slice(1);
}

/** "Dana Lee <dana@acme.example>" → "Dana Lee"; a bare address stays as it is. */
export function counterpartName(counterpart: string): string {
  const trimmed = counterpart.trim();
  const named = /^(.*?)\s*<[^>]*>$/.exec(trimmed);
  if (named && named[1].trim()) return named[1].trim().replace(/^"|"$/g, "");
  return trimmed.replace(/^<|>$/g, "");
}

/** "Email with Dana Lee", or "Email" when nobody is named. */
export function noteWhere(note: Pick<HistoryNote, "channel" | "counterpart">): string {
  const channel = note.channel === "email" ? "Email" : note.channel.charAt(0).toUpperCase() + note.channel.slice(1);
  const who = counterpartName(note.counterpart);
  return who ? `${channel} with ${who}` : channel;
}

/** What the note says now: their correction when they made one. */
export function noteText(note: Pick<HistoryNote, "summary" | "correction">): string {
  return note.correction?.trim() ? note.correction : note.summary;
}

/** When it goes, as the row says it: "Kept until you forget it" when pinned
 * or kept for good, else "Forgotten on 2026-12-30". */
export function noteExpiry(note: Pick<HistoryNote, "pinned" | "expires_at">): string {
  if (note.pinned) return "Pinned: kept until you forget it";
  if (!note.expires_at) return "Kept until you forget it";
  return `Forgotten on ${note.expires_at.slice(0, 10)}`;
}
