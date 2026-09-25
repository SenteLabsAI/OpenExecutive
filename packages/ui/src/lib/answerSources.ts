// Web chat: what an answer looked at, and which part of the analysis it had
// to leave out. Sent after the reply as a `sources` stream event and saved
// with it (see packages/core/openexecutive/orchestrator/answer_sources.py).
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/answerSources.test.mjs).

export type SourceKind = "company" | "knowledge" | "notion" | "research" | "document" | "web";

export interface AnswerSource {
  kind: SourceKind;
  title: string;
  url: string | null;
}

export interface AnswerSources {
  sources: AnswerSource[];
  /** Parts of the analysis (e.g. "finance") the reply went ahead without. */
  unavailable: string[];
}

// Display order and headings — one per SourceKind in answer_sources.py, and
// scripts/answerSources.test.mjs fails if the two ever disagree.
export const SOURCE_GROUPS: ReadonlyArray<{ kind: SourceKind; label: string }> = [
  { kind: "company", label: "Your documents" },
  { kind: "document", label: "Earlier documents" },
  { kind: "notion", label: "Notion" },
  { kind: "research", label: "Research notes" },
  { kind: "knowledge", label: "Built-in knowledge" },
  { kind: "web", label: "Web" },
];

export interface SourceGroup {
  kind: SourceKind;
  label: string;
  items: AnswerSource[];
}

/** The sources under their headings, in display order; empty groups and
 * unknown kinds are left out. */
export function groupSources(sources: readonly AnswerSource[]): SourceGroup[] {
  return SOURCE_GROUPS.map(({ kind, label }) => ({
    kind,
    label,
    items: sources.filter((s) => s.kind === kind && typeof s.title === "string" && s.title.trim()),
  })).filter((group) => group.items.length > 0);
}

// An in-app page such as /artifacts/alert%3A12 — nothing a browser could
// read as another site ("//host", "/\host").
const IN_APP_PATH = /^\/(?!\/)[A-Za-z0-9_%~/-]*$/;

/** Where a source links to, or null when it shouldn't be a link at all. */
export function sourceLink(url: string | null | undefined): { href: string; external: boolean } | null {
  if (!url) return null;
  if (url.startsWith("/")) return IN_APP_PATH.test(url) ? { href: url, external: false } : null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? { href: parsed.href, external: true } : null;
  } catch {
    return null;
  }
}

/** The site a web source is on, shown beside its title. */
export function sourceSite(url: string | null | undefined): string | null {
  const link = sourceLink(url);
  if (!link?.external) return null;
  return new URL(link.href).hostname.replace(/^www\./, "");
}

/** The line shown when part of the analysis is missing, or null. */
export function missingNote(unavailable: readonly string[]): string | null {
  const areas = unavailable.filter((area) => typeof area === "string" && area.trim());
  if (areas.length === 0) return null;
  return `Some of the analysis is missing (${areas.join(", ")}). Asking again may fill it in.`;
}
