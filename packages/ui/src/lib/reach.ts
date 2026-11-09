import type { DelegationSettings, OutsideContentMode, ReachStep } from "@/lib/api";

// Delegate is one page (app/delegate/page.tsx) in three blocks. How far it
// goes as you is one setting on the server (PUT /delegation/reach), shown as
// one picker in the As you block: Off, When you ask, Your inbox. What goes on
// its own from the inbox is the Approvals level's call (the Before anything
// goes out block, and Settings → Your Executive), held back by In training;
// there is no switch for it.

// Where each block sits, for links from Home, What it does for you and the
// older tabs' addresses.
export const AS_YOU_ANCHOR = "as-you";
export const AS_ME_ANCHOR = "as-me";
export const APPROVALS_ANCHOR = "before-anything-goes-out";
export const AS_YOU_HREF = `/delegate#${AS_YOU_ANCHOR}`;
export const AS_ME_HREF = `/delegate#${AS_ME_ANCHOR}`;
export const APPROVALS_HREF = `/delegate#${APPROVALS_ANCHOR}`;

export const REACH_ORDER: ReachStep[] = ["off", "when_asked", "inbox"];

// What goes on its own from your inbox at each Approvals level
// (delegation/handle_it.py, invites.py, action_cards.py), once In training
// lets it.
export const APPROVALS_SENDS: Record<OutsideContentMode, { label: string; line: string }> = {
  ask: {
    label: "Careful",
    line: "Nothing goes on its own. Every reply, follow-up, invite and suggested action waits for you.",
  },
  outsiders: {
    label: "Balanced",
    line: "Replies and follow-ups go when it's sure, to the people the email was already with. Invites and suggested actions go for your team and contacts.",
  },
  trust: {
    label: "Hands-off",
    line: "Replies and follow-ups go when it's sure, to the people the email was already with. Invites from people you've written to and suggested actions for anyone go too.",
  },
};

// What the Executive does in its own name at each level (orchestrator/take_the_lead.py).
export const APPROVALS_LEADS: Record<OutsideContentMode, string> = {
  ask: "Every action waits for your yes.",
  outsiders: "Your team and contacts go. Someone new, money, contracts, people decisions, deleting or sharing, and sends to more than five people wait.",
  trust: "Everything goes. Only your rules still wait.",
};

export function reachOf(settings: DelegationSettings): ReachStep {
  if (!settings.enabled) return "off";
  return settings.inbox?.enabled ? "inbox" : "when_asked";
}

// The "Now:" line under the As you picker.
export function reachLine(step: ReachStep, inTraining: boolean, level: OutsideContentMode | null): string {
  if (step === "off") return "It never writes as you.";
  if (step === "when_asked") return "Ask in chat and the draft waits in your Drafts. You send it.";
  if (inTraining) return "It drafts replies to your inbox. In training, every one waits for you until you allow that person.";
  if (level === "ask" || level === null) return "It drafts replies to your inbox. On Careful, every one waits for you.";
  return `It drafts replies to your inbox, and on ${APPROVALS_SENDS[level].label} sends the sure ones itself.`;
}

export function sentTodayLine(n: number): string {
  return n === 1 ? "Sent 1 reply on its own today." : `Sent ${n} replies on its own today.`;
}
