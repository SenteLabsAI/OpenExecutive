// A card named by its number ("card 220") tells the reader nothing. The
// server names cards by their headline before a chat app or the briefs send
// them (alerts/card_refs.py); a web chat reply streams as it is written, so a
// number that still slips through is shown here as a link to that card on
// Home (`/?card=<id>`, components/Briefing.tsx) instead.

// "card 220", "Card #220", "the card 220", "alert 220". The word must come
// right before the number, so "/cards/220" and links stay as written.
const CARD_MENTION = /(?<![\w/[])(?:(the|that)\s+)?\b(card|alert)(?:\s*#\s*|\s+)(\d{1,9})\b(?!\]|\()/gi;

/** `text` (Markdown) with each "card 220" written as a link to that card on Home. */
export function linkCardMentions(text: string): string {
  if (!text) return text;
  return text.replace(CARD_MENTION, (_match, the: string | undefined, word: string, id: string) => {
    const lead = the ?? word;
    const label = /^[A-Z]/.test(lead) ? "That card" : "that card";
    return `[${label}](/?card=${id})`;
  });
}

/** The card id a Home link asks for (`?card=220`), or null. */
export function cardFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get("card");
  return raw && /^\d{1,9}$/.test(raw) ? Number(raw) : null;
}
