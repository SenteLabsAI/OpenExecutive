import type { LeadRule, LeadRuleKind } from "@/lib/api";

// Your rules (orchestrator/take_the_lead.py): the four kinds, and the one
// line Delegate shows while the list is closed. Pure, so scripts/*.test.mjs
// can load it.

export const RULE_KINDS: { kind: LeadRuleKind; label: string; placeholder: string; describe: (v: string) => string }[] = [
  { kind: "person", label: "A person", placeholder: "Name or email", describe: (v) => `Anything to or about ${v}` },
  { kind: "domain", label: "A company", placeholder: "example.com", describe: (v) => `Anything to someone at ${v}` },
  { kind: "words", label: "Words", placeholder: "acquisition", describe: (v) => `Anything that mentions “${v}”` },
  { kind: "amount", label: "An amount", placeholder: "500", describe: (v) => `Any amount of ${v} or more, written with a currency ($, €, USD…)` },
];

export function describeRule(rule: Pick<LeadRule, "kind" | "value">): string {
  return RULE_KINDS.find((r) => r.kind === rule.kind)?.describe(rule.value) ?? rule.value;
}

// Closed: the first rule and how many more, or what a rule is for.
export function rulesSummary(rules: Pick<LeadRule, "kind" | "value">[]): string {
  if (rules.length === 0) return "None yet. A rule makes something always wait for your yes, on every level.";
  const first = describeRule(rules[0]);
  if (rules.length === 1) return `${first}. Always waits for your yes.`;
  return `${first}, and ${rules.length - 1} more. Always wait for your yes.`;
}
