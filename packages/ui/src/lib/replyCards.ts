// The reply cards on Today (GET /delegation/replies): the wording for who
// the sender is to you and for each warning a card carries, kept apart from
// the component so `npm test` can check it (see scripts/replyCards.test.mjs).
// The flags come from delegation/inbox.py (card_flags, the reconciler),
// delegation/threads.py (plan_reply) and delegation/ghostwriter.py (lint).

const RELATION_TEXT: Record<string, string> = {
  team: "Your team",
  contact: "A contact",
  correspondent: "You've written to them before",
  stranger: "New to you",
};

/** Who the sender is to you, or "" for a relation this UI doesn't know. */
export function relationLabel(relation: string): string {
  return RELATION_TEXT[relation] ?? "";
}

// asks_if_ai is left out: the card's open questions already say it.
const FLAG_TEXT: Record<string, string> = {
  sender_unverified: "Gmail couldn't confirm this came from that address. Check it before you reply.",
  thread_moved_on: "A newer message arrived in this thread after the draft was written.",
  others_on_thread: "Others were on this email. The draft goes to the sender only.",
  executive_on_thread: "The Executive was on this email too.",
  reply_to_ignored: "The email asked for replies to go elsewhere. The draft goes to the sender.",
  mailing_list: "This came through a mailing list.",
  removed_link: "A link was taken out of the draft.",
  removed_address: "An email address was taken out of the draft.",
  names_the_executive: "The draft mentions the Executive by name.",
  shortened: "The draft was cut to length.",
};

// The order the warnings show in: the ones to act on first.
const FLAG_ORDER = Object.keys(FLAG_TEXT);

/** A card's warnings in plain words, most pressing first, each once.
 * Flags this UI doesn't know are left out rather than shown raw. */
export function replyFlagLines(flags: readonly string[]): string[] {
  const seen = new Set(flags);
  return FLAG_ORDER.filter((f) => seen.has(f)).map((f) => FLAG_TEXT[f]);
}

/** "Dana Park <dana@…>" as the card's From line; the address alone when
 * there is no name. */
export function senderLine(card: { from_name: string; from_email: string }): string {
  const name = card.from_name.trim();
  return name ? `${name} <${card.from_email}>` : card.from_email;
}

/** The card's Gmail link, only when it really opens Gmail: the backend
 * builds it from a fixed prefix, and the page never links anywhere else. */
export function safeGmailLink(link: string): string {
  return link.startsWith("https://mail.google.com/") ? link : "";
}
