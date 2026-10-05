// Settings → About you: how the card of what peer memory learned about the
// signed-in person reads. The memory service writes its card lines with a
// category prefix ("ATTRIBUTE: Email: …") and its notes name the person by
// their bare peer id ("peer 3 asked …", "3 asked …"); neither means anything
// to the person reading about themselves. Kept free of React so scripts/ can
// test it.

export type CardEntry = { label: string; value: string } | { label: null; value: string };

// The memory service's category tags: all caps, at the start of the line.
const CATEGORY = /^[A-Z][A-Z_ ]{1,30}:\s*/;
// A short "Label: value" pair after the tag, e.g. "Email: a@b.com".
const LABELLED = /^([^:]{1,24}):\s+(\S.*)$/;

/** One card line as a labelled row when it is a "Label: value" pair, else plain text. */
export function cardEntry(line: string): CardEntry {
  const text = line.trim().replace(CATEGORY, "");
  const m = LABELLED.exec(text);
  if (m && !/^https?$/i.test(m[1])) return { label: m[1].trim(), value: m[2].trim() };
  return { label: null, value: text };
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * A note about the person with their peer id swapped for their first name
 * (the notes are written in the third person, so "you" would read "you
 * likes"), and without a leading "On 2026-10-01, " (the row shows the date).
 */
export function noteAboutYou(text: string, personId: number | string, fullName: string): string {
  const id = escapeRegExp(String(personId));
  const name = fullName.trim().split(/\s+/)[0] || "You";
  let out = text.trim().replace(/^On \d{4}-\d{2}-\d{2},\s*/, "");
  out = out
    .replace(new RegExp(`\\bpeer[ _-]?${id}\\b`, "gi"), name)
    .replace(new RegExp(`^${id}(?=\\s|'s\\b)`), name);
  return out.charAt(0).toUpperCase() + out.slice(1);
}
