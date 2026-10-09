// Searching inside chats: the Chats page shows where the words were found,
// and an opened chat steps through the messages that contain them.

export interface TextPart {
  text: string;
  match: boolean;
}

/** `text` cut into runs, marking every case-insensitive occurrence of `query`. */
export function highlightParts(text: string, query: string): TextPart[] {
  const q = query.trim().toLowerCase();
  if (!q) return text ? [{ text, match: false }] : [];
  const lower = text.toLowerCase();
  const parts: TextPart[] = [];
  let at = 0;
  for (;;) {
    const hit = lower.indexOf(q, at);
    if (hit < 0) break;
    if (hit > at) parts.push({ text: text.slice(at, hit), match: false });
    parts.push({ text: text.slice(hit, hit + q.length), match: true });
    at = hit + q.length;
  }
  if (at < text.length) parts.push({ text: text.slice(at), match: false });
  return parts;
}

/** What a search result row says under its title, or null outside a search. */
export function matchLabel(matchCount: number | undefined): string | null {
  if (matchCount === undefined) return null;
  if (matchCount === 0) return "title match";
  return `${matchCount} match${matchCount === 1 ? "" : "es"}`;
}

/** Indexes of the messages whose text contains `query`, in order. */
export function matchingMessages(messages: { content: unknown }[], query: string): number[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const out: number[] = [];
  messages.forEach((m, i) => {
    if (typeof m.content === "string" && m.content.toLowerCase().includes(q)) out.push(i);
  });
  return out;
}
