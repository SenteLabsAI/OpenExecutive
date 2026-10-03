// What an Agent Council card shows for one agent (app/council/page.tsx):
// a short name, its area in plain words, initials for the avatar, a short
// model name and one status line. Kept pure so `npm test` can check it
// (scripts/councilCards.test.mjs).

// Plain-word areas for the built-in agents. An agent missing here (a new
// specialist) falls back to its role's grouping or its knowledge domains.
const AREAS: Record<string, string> = {
  executive: "Leads every answer",
  cso: "Strategy",
  cfo: "Finance",
  chro: "People",
  gc: "Legal",
  coo: "Operations",
  cmo: "Marketing",
  cpo: "Product",
  sales: "Sales",
  board_comms: "Board and investors",
  triage: "Sorts incoming messages",
  quality_judge: "Reviews committee answers",
  utility_fast: "Quick checks, titles and replies",
  research: "Background research scans",
  fixture_generator: "Builds sample companies",
  engagement_intake: "Drafts client companies",
  onboarding_interviewer: "Company setup interview",
  workflow_designer: "Designs new workflows",
  workflow_actor: "Runs workflow steps",
};

const DOMAIN_WORDS: Record<string, string> = { hr: "people" };

const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// "Utility · Fast model (gates, titles, decision parsing)" → group "Utility",
// name "Fast model", note "gates, titles, decision parsing".
function splitRole(role: string): { group: string | null; name: string; note: string | null } {
  let rest = role.trim();
  let group: string | null = null;
  const dot = rest.indexOf(" · ");
  if (dot > 0) {
    group = rest.slice(0, dot).trim();
    rest = rest.slice(dot + 3).trim();
  }
  let note: string | null = null;
  const paren = rest.match(/^(.*?)\s*\((.+)\)\s*$/);
  if (paren && paren[1]) {
    rest = paren[1];
    note = paren[2];
  }
  return { group, name: rest || role.trim(), note };
}

/** The card's title: the role without its "Utility ·" group or (note). */
export function agentDisplayName(role: string): string {
  return splitRole(role).name;
}

/** What the agent covers, in a few plain words. */
export function agentArea(agent: { name: string; role: string; domains: string[] }): string {
  const known = AREAS[agent.name];
  if (known) return known;
  const { group, note } = splitRole(agent.role);
  if (note) return capitalize(note);
  if (agent.domains.length > 0) {
    return capitalize(agent.domains.map((d) => DOMAIN_WORDS[d] ?? d.replace(/_/g, " ")).join(" and "));
  }
  return group ?? "Specialist";
}

const SMALL_WORDS = new Set(["of", "the", "and", "&", "for", "a", "an"]);

/** Two letters for the avatar: "Chief Financial Officer" → "CF", "Executive" → "EX". */
export function agentInitials(name: string): string {
  const words = name
    .split(/[\s/·-]+/)
    .map((w) => w.replace(/[^\p{L}\p{N}]/gu, ""))
    .filter((w) => w && !SMALL_WORDS.has(w.toLowerCase()));
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/**
 * A short model name: "Sonnet 5" for claude-sonnet-5, "Opus 5.5" for
 * claude-opus-5-5 or anthropic/claude-opus-5.5, "gpt-5" for openai/gpt-5.
 * `label` is the picker's label for the id when known ("Claude Sonnet 5").
 */
export function shortModelName(id: string, label?: string): string {
  if (label && label !== id) return label.replace(/^Claude\s+/i, "");
  const bare = id.replace(/^anthropic\//i, "");
  const claude = bare.match(/^claude-([a-z]+)-([0-9][0-9.-]*?)(?:-\d{8})?$/i);
  if (claude) return `${capitalize(claude[1].toLowerCase())} ${claude[2].replace(/-/g, ".")}`;
  const slash = bare.lastIndexOf("/");
  return slash >= 0 ? bare.slice(slash + 1) : bare;
}

// Override fields that only a Quality preset also writes.
const PRESET_FIELDS = new Set(["model", "use_deep_reasoning"]);

export type AgentCardStatus = "instructions" | "custom-model" | "default";

/**
 * The card's status line. "instructions" when the owner changed anything a
 * preset doesn't touch (instructions, prompt, role, research focus);
 * "custom-model" when only the model or deep reasoning differs from the
 * active Quality preset; otherwise "default" (the model follows Quality).
 * `overriddenFields` is undefined when the agent's detail wasn't loaded; an
 * override then counts as the owner's own.
 */
export function agentCardStatus(
  hasOverride: boolean,
  overriddenFields: readonly string[] | undefined,
  differsFromPreset: boolean,
): AgentCardStatus {
  if (!hasOverride) return differsFromPreset ? "custom-model" : "default";
  if (overriddenFields === undefined) return "instructions";
  if (overriddenFields.some((f) => !PRESET_FIELDS.has(f))) return "instructions";
  return differsFromPreset ? "custom-model" : "default";
}

/**
 * The agents "Your agents" lists: the core ones, or every agent (the
 * internal and helper ones too) once the owner clicks "Show all agents".
 */
export function listedAgents<T extends { visibility: string }>(agents: readonly T[], showAll: boolean): T[] {
  return showAll ? [...agents] : agents.filter((a) => a.visibility === "core");
}

// The helper agents that are only a model setting: they have no instructions
// to edit, so the simple editor has nothing for them.
const MODEL_ONLY_AGENTS = new Set(["utility_fast", "research"]);

/** True for a helper agent with no instructions to edit (only a model). */
export function agentHasNoInstructions(name: string): boolean {
  return MODEL_ONLY_AGENTS.has(name);
}

/**
 * Whether the agent panel shows the full tabbed editor: when this browser
 * prefers it, and always for a helper agent, whose settings live only there.
 */
export function panelOpensAdvanced(name: string, prefersAdvanced: boolean): boolean {
  return prefersAdvanced || agentHasNoInstructions(name);
}
