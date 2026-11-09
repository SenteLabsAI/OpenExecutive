"use client";

import { useState } from "react";

import type { LeadRule, LeadRuleEffect, LeadRuleKind } from "@/lib/api";
import { EFFECT_HINT, RULE_KINDS, RULES_ORDER, describeRule, kindGoes, ruleGoes } from "@/lib/leadRules";

// Your rules (orchestrator/take_the_lead.py), the owner's, on Delegate →
// Approvals. A Waits rule always waits for a yes, on every level; a Goes
// rule lets something go on its own where the level would hold it. Both
// cover what it does as you and as me. The list shows while closed; the
// form opens with Add a rule.
const EFFECTS: { effect: LeadRuleEffect; label: string }[] = [
  { effect: "wait", label: "Always wait for me" },
  { effect: "go", label: "Goes on its own" },
];

export function RuleBadge({ goes }: { goes: boolean }) {
  return (
    <span
      className={`flex-none rounded-md px-2 py-0.5 text-xs font-semibold ${
        goes
          ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300"
          : "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300"
      }`}
    >
      {goes ? "Goes" : "Waits"}
    </span>
  );
}

export default function LeadRulesEditor({
  rules,
  onAdd,
  onDelete,
  adding,
  onDone,
  disabled = false,
}: {
  rules: LeadRule[];
  onAdd: (kind: LeadRuleKind, value: string, effect: LeadRuleEffect) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  /** The Add a rule form is open. */
  adding: boolean;
  onDone: () => void;
  disabled?: boolean;
}) {
  const [effect, setEffect] = useState<LeadRuleEffect>("go");
  const [kind, setKind] = useState<LeadRuleKind>("amount");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const spec = RULE_KINDS.find((r) => r.kind === kind) ?? RULE_KINDS[0];

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save the rule.");
    } finally {
      setBusy(false);
    }
  };

  const segment = (on: boolean) =>
    `min-h-touch px-3 text-[14px] border-r border-line last:border-r-0 ${
      on ? "bg-accent/10 font-semibold text-accent" : "text-fg hover:bg-surface-overlay"
    }`;

  return (
    <div className="flex flex-col gap-3">
      {rules.length > 0 && (
        <>
          <ul className="flex flex-col gap-2" aria-label="Rules">
            {rules.map((rule) => (
              <li
                key={rule.id}
                className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-[15px] ${
                  ruleGoes(rule) ? "border-emerald-300/70 dark:border-emerald-500/40" : "border-amber-300/70 dark:border-amber-500/40"
                }`}
              >
                <span className="flex min-w-0 items-center gap-2">
                  <RuleBadge goes={ruleGoes(rule)} />
                  <span className="min-w-0 break-words">{describeRule(rule)}</span>
                </span>
                <button
                  type="button"
                  onClick={() => void run(() => onDelete(rule.id))}
                  disabled={busy || disabled}
                  className="min-h-touch flex-shrink-0 px-2 text-sm text-accent hover:underline disabled:opacity-50"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <ol className="list-decimal space-y-0.5 pl-5 text-[13px] leading-relaxed text-fg-muted">
            {RULES_ORDER.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ol>
        </>
      )}
      {adding && (
        <form
          id="rules-editor"
          className="flex flex-col gap-3 rounded-xl border border-line p-4"
          onSubmit={(e) => {
            e.preventDefault();
            const text = value.trim();
            if (!text) return;
            void run(async () => {
              await onAdd(kind, text, effect);
              setValue("");
              onDone();
            });
          }}
        >
          <fieldset className="flex flex-col gap-1.5">
            <legend className="mb-1.5 text-[13px] font-semibold text-fg">What should happen</legend>
            <div className="inline-flex w-fit overflow-hidden rounded-lg border border-line">
              {EFFECTS.map((e) => (
                <button
                  key={e.effect}
                  type="button"
                  aria-pressed={effect === e.effect}
                  onClick={() => {
                    setEffect(e.effect);
                    if (e.effect === "go" && !kindGoes(kind)) setKind("amount");
                  }}
                  disabled={busy || disabled}
                  className={segment(effect === e.effect)}
                >
                  {e.label}
                </button>
              ))}
            </div>
          </fieldset>
          <fieldset className="flex flex-col gap-1.5">
            <legend className="mb-1.5 text-[13px] font-semibold text-fg">When it&apos;s about</legend>
            <div className="inline-flex w-fit flex-wrap overflow-hidden rounded-lg border border-line">
              {RULE_KINDS.filter((r) => effect === "wait" || kindGoes(r.kind)).map((r) => (
                <button
                  key={r.kind}
                  type="button"
                  aria-pressed={kind === r.kind}
                  onClick={() => setKind(r.kind)}
                  disabled={busy || disabled}
                  className={segment(kind === r.kind)}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </fieldset>
          <div className="flex flex-col gap-1.5">
            <label className="text-[13px] font-semibold text-fg" htmlFor="lead-rule-value">
              {kind === "amount" ? (effect === "go" ? "Under" : "This much or more") : spec.label}
            </label>
            <input
              id="lead-rule-value"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={spec.placeholder}
              maxLength={200}
              inputMode={kind === "amount" ? "decimal" : undefined}
              disabled={busy || disabled}
              className="min-h-touch w-full max-w-sm rounded-lg border border-line bg-surface px-3 text-[15px]"
            />
          </div>
          <p className="text-sm text-fg-muted">{EFFECT_HINT[effect]}</p>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || disabled || !value.trim()}
              className="min-h-touch rounded-lg bg-accent px-4 text-[15px] font-semibold text-white disabled:opacity-50"
            >
              Add rule
            </button>
            <button
              type="button"
              onClick={onDone}
              className="min-h-touch rounded-lg border border-line px-4 text-[15px] font-medium text-fg hover:bg-surface-overlay"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
      {error && <p className="text-sm text-red-500">{error}</p>}
    </div>
  );
}
