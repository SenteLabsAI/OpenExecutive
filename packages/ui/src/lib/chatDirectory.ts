// The Slack, Discord and Telegram people and rooms the bots can reach, as the
// pickers on the People and department pages use them (GET /chat-directory/*).
// No imports, so `npm test` can load it (scripts/chatDirectory.test.mjs).

export interface DirectoryOption {
  /** What gets saved (a Slack, Discord or Telegram id). Never shown. */
  id: string;
  name: string;
  /** "@handle", "Private", the Discord server, … */
  detail: string;
  email?: string | null;
}

export interface ProviderList {
  /** False when this instance runs no bot for the provider. */
  configured: boolean;
  /** False when the provider could not be read just now. */
  ok: boolean;
  options: DirectoryOption[];
  truncated: boolean;
  /** Discord people only: false when Discord won't list server members. */
  members_available: boolean | null;
}

export interface DirectoryListing {
  slack: ProviderList;
  discord: ProviderList;
  telegram: ProviderList;
}

export type ChatProvider = keyof DirectoryListing;

export function findOption(options: readonly DirectoryOption[], id: string): DirectoryOption | null {
  const want = id.trim();
  if (!want) return null;
  return options.find((o) => o.id === want) ?? null;
}

function words(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/&/g, " ")
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
}

/** The options whose name, detail or email holds every word typed. */
export function filterOptions(
  options: readonly DirectoryOption[],
  query: string,
  limit = 50,
): DirectoryOption[] {
  const want = words(query);
  const out: DirectoryOption[] = [];
  for (const o of options) {
    const hay = `${o.name} ${o.detail} ${o.email ?? ""}`.toLowerCase();
    if (want.every((w) => hay.includes(w))) out.push(o);
    if (out.length >= limit) break;
  }
  return out;
}

// Plurals and common suffixes, so "Operations" meets #ops and "Sales" #sale.
const ALIASES: Record<string, string> = {
  operations: "ops",
  operation: "ops",
  engineering: "eng",
  marketing: "mktg",
  finance: "fin",
  product: "prod",
  people: "hr",
  human: "hr",
  resources: "hr",
  customer: "cs",
  success: "cs",
};

function stem(w: string): string {
  return w.length > 3 && w.endsWith("s") ? w.slice(0, -1) : w;
}

function forms(w: string): string[] {
  const s = stem(w);
  return ALIASES[w] ? [s, ALIASES[w]] : [s];
}

/**
 * The room whose name best matches a department's ("Sales" → #sales,
 * "Customer Success" → #customer-success), or null when nothing shares a
 * word with it. A suggestion only: the page saves it when the person does.
 */
export function closestOption(
  deptName: string,
  options: readonly DirectoryOption[],
): DirectoryOption | null {
  const target = words(deptName).flatMap(forms);
  if (target.length === 0) return null;
  const joined = words(deptName).join("");
  let best: DirectoryOption | null = null;
  let bestScore = 0;
  for (const o of options) {
    const name = words(o.name);
    if (name.length === 0) continue;
    const have = new Set(name.flatMap(forms));
    let score = name.join("") === joined ? 10 : 0;
    const shared = target.filter((w) => have.has(w)).length;
    // Shared words count most; a long channel name with one shared word
    // ranks below a short one.
    score += shared * 3 - (name.length - shared) * 0.5;
    if (shared === 0 && score < 10) continue;
    if (score > bestScore) {
      best = o;
      bestScore = score;
    }
  }
  return best;
}
