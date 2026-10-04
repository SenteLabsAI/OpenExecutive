// The letters on someone's avatar: first and last name's first letters
// ("Jordan Avery" → "JA"); a single name's first two ("Jordan" → "JO"). With
// only an email, its local part is split on dots, dashes and underscores the
// same way ("jordan.avery@x" → "JA"). No imports, so `npm test` can load it.

export function initials(nameOrEmail: string): string {
  const raw = nameOrEmail.trim();
  const base = raw.includes("@") && !/\s/.test(raw) ? raw.split("@")[0].replace(/[._-]+/g, " ") : raw;
  const words = base.split(/\s+/).filter((w) => /\p{L}|\p{N}/u.test(w));
  if (words.length === 0) return "?";
  const first = (w: string) => Array.from(w.replace(/^[^\p{L}\p{N}]+/u, ""))[0] ?? "";
  const out = words.length === 1 ? Array.from(words[0]).slice(0, 2).join("") : first(words[0]) + first(words[words.length - 1]);
  return out.toUpperCase() || "?";
}
