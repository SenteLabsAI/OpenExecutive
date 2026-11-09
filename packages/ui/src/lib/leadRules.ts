import type { LeadRule, LeadRuleEffect, LeadRuleKind } from "@/lib/api";

// Your rules (orchestrator/take_the_lead.py): the four kinds, each either
// Waits (always waits for your yes, on every level) or Goes (goes on its own
// where the level would hold it), and the lines Delegate → Approvals shows.
// Pure, so scripts/*.test.mjs can load it.

// 5000 → "5,000". The rule holds a plain number; any currency counts.
function money(v: string): string {
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("en-US") : v;
}

type Rule = Pick<LeadRule, "kind" | "value"> & { effect?: LeadRuleEffect };

export const RULE_KINDS: {
  kind: LeadRuleKind;
  label: string;
  placeholder: string;
  describe: (v: string) => string;
  goes: (v: string) => string;
}[] = [
  {
    kind: "amount",
    label: "An amount",
    placeholder: "500",
    describe: (v) => `Any amount of ${money(v)} or more`,
    goes: (v) => `Amounts under ${money(v)}`,
  },
  {
    kind: "person",
    label: "A person",
    placeholder: "Name or email",
    describe: (v) => `Anything to or about ${v}`,
    goes: (v) => `Anything only to ${v}`,
  },
  {
    kind: "domain",
    label: "A company",
    placeholder: "example.com",
    describe: (v) => `Anything to someone at ${v}`,
    goes: (v) => `Anything only to people at ${v}`,
  },
  {
    kind: "words",
    label: "Words",
    placeholder: "acquisition",
    describe: (v) => `Anything that mentions “${v}”`,
    goes: (v) => `Anything that mentions “${v}”`,
  },
];

// What a Goes rule can be about (GOES_RULE_KINDS in take_the_lead.py).
// Words only wait: what an email says can end up in what it sends.
export function kindGoes(kind: LeadRuleKind): boolean {
  return kind !== "words";
}

export function ruleGoes(rule: Rule): boolean {
  return rule.effect === "go";
}

export function describeRule(rule: Rule): string {
  const spec = RULE_KINDS.find((r) => r.kind === rule.kind);
  if (!spec) return rule.value;
  return ruleGoes(rule) ? spec.goes(rule.value) : spec.describe(rule.value);
}

function counts(rules: Rule[]): { go: number; wait: number } {
  const go = rules.filter(ruleGoes).length;
  return { go, wait: rules.length - go };
}

function countsLine(rules: Rule[]): string {
  const { go, wait } = counts(rules);
  return [
    go ? `${go} ${go === 1 ? "goes" : "go"} on ${go === 1 ? "its" : "their"} own` : null,
    wait ? `${wait} always ${wait === 1 ? "waits" : "wait"}` : null,
  ]
    .filter(Boolean)
    .join(", ");
}

// The Your rules card's line.
export function rulesSummary(rules: Rule[]): string {
  if (rules.length === 0) {
    return "None yet. Approvals decides everything. Add a rule to make something always wait for your yes, or to let something go on its own.";
  }
  return "A Waits rule always waits for your yes. A Goes rule goes on its own where Approvals would ask.";
}

// Under the Approvals level, only once there are rules: they change what
// the level line says.
export function rulesChangeLine(rules: Rule[]): string | null {
  return rules.length === 0 ? null : `${countsLine(rules)}.`;
}

// The order rules are read in, shown under the list.
export const RULES_ORDER: string[] = [
  "Careful holds everything.",
  "A Waits rule wins over a Goes rule.",
  "A Goes rule lets it through, or else Approvals decides.",
  "Last check, In training: while it's on, nothing goes out on its own, rules included. Only what you ticked “on its own next time” goes.",
];

// Under the form, for the effect picked.
export const EFFECT_HINT: Record<LeadRuleEffect, string> = {
  wait: "Always waits for your yes, on every level.",
  go: "Goes without asking on Balanced and Hands-off. Careful and In training still hold it.",
};
