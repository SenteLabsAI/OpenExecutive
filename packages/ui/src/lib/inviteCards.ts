// Pure helpers for the meeting invite cards on Home (components/InviteCards.tsx):
// an invite from someone you know, to accept or decline in your own calendar
// (Act as me, with Read my calendar on). No React here so `npm test` can
// exercise it directly (scripts/inviteCards.test.mjs).

export interface InviteCardLike {
  from_name: string;
  from_email: string;
  clashes: string[];
  clash_checked: boolean;
}

/** Who invited you, by first name (their address when there's no name). */
export function inviterFirstName(card: { from_name: string; from_email: string }): string {
  return card.from_name.trim().split(/\s+/)[0] || card.from_email;
}

/** The training tick's words, as on a reply card. */
export function inviteNextTimeLabel(card: { from_name: string; from_email: string }): string {
  return `Accept from ${inviterFirstName(card)} on its own next time`;
}

/** What the card says about your calendar at that time: a clash (times
 *  only), that it couldn't look, or nothing when it's free. */
export function clashLine(card: InviteCardLike): string {
  if (!card.clash_checked) return "Couldn't check your calendar for clashes.";
  if (card.clashes.length === 0) return "";
  const times = card.clashes.slice(0, 3).join(", ");
  const more = card.clashes.length > 3 ? ` and ${card.clashes.length - 3} more` : "";
  return `Clashes with ${card.clashes.length === 1 ? "something" : "other things"} on your calendar: ${times}${more}.`;
}

/** The line once you answered. */
export function answeredLine(answer: "accept" | "decline", card: { from_name: string; from_email: string }): string {
  const who = inviterFirstName(card);
  return answer === "accept"
    ? `Accepted. Your calendar lets ${who} know.`
    : `Declined. Your calendar lets ${who} know.`;
}

/** One line for something Handle it for me did (What it sent): an accepted
 *  invite says so; a reply names who it went to. */
export function handledLine(h: { source?: string; summary?: string; to_name: string; to_email: string; subject: string }): string {
  if (h.source === "invite" && h.summary) return h.summary;
  const verb = h.source === "follow_up" ? "Followed up with" : "Replied to";
  return `${verb} ${h.to_name || h.to_email}: ${h.subject}`;
}
