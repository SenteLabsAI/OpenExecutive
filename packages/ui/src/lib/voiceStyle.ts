// Settings → Act as me → How I write: the style as a few plain sentences
// instead of a form, which of them a described style adds, and the phrases a
// person can tap into their description. Kept free of React so scripts/ can
// test it.

export interface VoiceStyle {
  greetings: Record<string, string>;
  sign_off: string;
  length: string;
  formality: string;
  habits: string[];
  avoid: string[];
}

// The audiences a greeting is learned for (delegation/voice.py AUDIENCES).
const AUDIENCE_PHRASE: Record<string, string> = {
  team: "your team",
  contact: "your contacts",
  other: "anyone else",
};
const LENGTH_WORD: Record<string, string> = { short: "short", medium: "medium-length", long: "long" };
const FORMALITY_WORD: Record<string, string> = { casual: "casual", neutral: "neutral", formal: "formal" };

// Phrases a person can tap to add to their description.
export const STYLE_PHRASES = [
  "Short and to the point",
  "Warm and friendly",
  "More formal with clients",
  "First names, never Dear",
  "No emojis",
  "No exclamation marks",
  "Bullet points for steps",
] as const;

function sentence(text: string): string {
  const t = text.trim();
  if (!t) return "";
  const first = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(first) ? first : `${first}.`;
}

// A greeting as a person reads it: {first} is their recipient's first name.
function shown(greeting: string): string {
  return greeting.replaceAll("{first}", "[first name]");
}

/** The style as plain sentences, in the order drafts use them. */
export function styleSentences(style: VoiceStyle): string[] {
  const out: string[] = [];
  const length = LENGTH_WORD[style.length];
  const formality = FORMALITY_WORD[style.formality];
  if (length && formality) out.push(`Usually ${length}, ${formality} emails.`);
  else if (length || formality) out.push(`Usually ${length ?? formality} emails.`);

  const greetings = Object.entries(style.greetings).filter(([a, g]) => AUDIENCE_PHRASE[a] && g.trim());
  const distinct = new Set(greetings.map(([, g]) => g.trim()));
  if (greetings.length === Object.keys(AUDIENCE_PHRASE).length && distinct.size === 1) {
    out.push(`Opens with "${shown(greetings[0][1].trim())}".`);
  } else {
    for (const [audience, greeting] of greetings) {
      out.push(`Opens with "${shown(greeting.trim())}" to ${AUDIENCE_PHRASE[audience]}.`);
    }
  }
  const signOff = style.sign_off
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .join(" ");
  if (signOff) out.push(`Signs off "${signOff}".`);
  for (const rule of [...style.habits, ...style.avoid]) {
    const s = sentence(rule);
    if (s) out.push(s);
  }
  return out;
}

/** Each sentence of `next`, marked new when `current` doesn't already say it. */
export function markNew(current: VoiceStyle, next: VoiceStyle): { text: string; isNew: boolean }[] {
  const had = new Set(styleSentences(current).map((s) => s.toLowerCase()));
  return styleSentences(next).map((text) => ({ text, isNew: !had.has(text.toLowerCase()) }));
}

/** The description with `phrase` added as its own sentence (once). */
export function addPhrase(text: string, phrase: string): string {
  const t = text.trimEnd();
  if (t.toLowerCase().includes(phrase.toLowerCase())) return text;
  if (!t) return `${phrase}.`;
  return `${t}${/[.!?]$/.test(t) ? "" : "."} ${phrase}.`;
}
