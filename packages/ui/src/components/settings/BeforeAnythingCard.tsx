"use client";

import Link from "next/link";
import { useState } from "react";

import LeadRulesEditor, { RULE_KINDS } from "@/components/settings/LeadRulesEditor";
import OutsideContentCard from "@/components/settings/OutsideContentCard";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  addCompanyLeadRule,
  deleteCompanyLeadRule,
  type OutsideContentMode,
  type OutsideContentSetting,
  type TakeTheLead,
} from "@/lib/api";
import { APPROVALS_LEADS, APPROVALS_SENDS } from "@/lib/reach";

// Delegate → Before anything goes out: the one setting for both sides.
// Approvals (the owner's; everyone else sees the level) decides what goes on
// its own and what comes as an Approve card on Home; Your rules (the
// owner's, POST/DELETE /take-the-lead/rules) always wait, on every level.

const LEVELS: OutsideContentMode[] = ["ask", "outsiders", "trust"];

export default function BeforeAnythingCard({
  approvals,
  lead,
  setLead,
}: {
  approvals: OutsideContentSetting | null;
  lead: TakeTheLead | null;
  setLead: (next: TakeTheLead) => void;
}) {
  const [editing, setEditing] = useState(false);
  const rules = lead?.rules ?? [];
  const first = rules[0];
  const firstLine = first ? (RULE_KINDS.find((r) => r.kind === first.kind)?.describe(first.value) ?? first.value) : null;

  return (
    <>
      <OutsideContentCard compact>
        {approvals?.can_change && (
          <>
            <ul className="grid gap-3 sm:grid-cols-3">
              {LEVELS.map((mode) => (
                <li
                  key={mode}
                  className={`rounded-xl border p-4 ${mode === approvals.mode ? "border-accent/60 bg-accent/10" : "border-line"}`}
                >
                  <span className="block text-[15px] font-semibold text-fg">{APPROVALS_SENDS[mode].label}</span>
                  <span className="mt-2 block text-[13px] leading-relaxed text-fg-muted">
                    <span className="font-semibold text-fg">As you:</span> {APPROVALS_SENDS[mode].line}
                  </span>
                  <span className="mt-2 block text-[13px] leading-relaxed text-fg-muted">
                    <span className="font-semibold text-fg">As me:</span> {APPROVALS_LEADS[mode]}
                  </span>
                </li>
              ))}
            </ul>
            <p className="text-sm text-fg-muted">
              In training, on either side, holds everything for you first, whatever the level.
            </p>
          </>
        )}
      </OutsideContentCard>
      {approvals && !approvals.can_change && (
        <SettingsCard
          title="Approvals"
          description={`On ${APPROVALS_SENDS[approvals.mode].label}. The account owner sets it, the same for everyone and on every channel.`}
        >
          <p className="text-sm text-fg-muted leading-relaxed">
            As you: {APPROVALS_SENDS[approvals.mode].line} As me: {APPROVALS_LEADS[approvals.mode]}
          </p>
        </SettingsCard>
      )}
      {lead && (
        <SettingsCard
          title="Your rules"
          titleId="rules-label"
          description={
            editing
              ? "Anything the Executive would do in its own name that matches one of these always waits for your yes, on every level."
              : rules.length === 0
                ? "None yet. A rule makes something always wait for your yes, on every level."
                : rules.length === 1
                  ? `${firstLine}. Always waits for your yes.`
                  : `${firstLine}, and ${rules.length - 1} more. Always wait for your yes.`
          }
          action={
            <button
              type="button"
              aria-expanded={editing}
              aria-controls="rules-editor"
              onClick={() => setEditing((v) => !v)}
              className="text-sm font-medium text-accent hover:underline cursor-pointer"
            >
              {editing ? "Done" : rules.length === 0 ? "Add a rule" : `Edit (${rules.length})`}
            </button>
          }
        >
          {editing && (
            <div id="rules-editor">
              <LeadRulesEditor
                rules={rules}
                emptyText="No rules yet."
                onAdd={async (kind, value) => setLead(await addCompanyLeadRule(kind, value))}
                onDelete={async (id) => setLead(await deleteCompanyLeadRule(id))}
              />
            </div>
          )}
        </SettingsCard>
      )}
      <p className="text-[15px] text-fg-muted">
        Pause, what it always does without asking, and the voice it answers in are under{" "}
        <Link href="/settings/executive" className="font-medium text-accent underline-offset-2 hover:underline">
          Settings → Your Executive
        </Link>
        .
      </p>
    </>
  );
}
