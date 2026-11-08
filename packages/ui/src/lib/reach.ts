import type { DelegationSettings, OutsideContentMode, ReachStep } from "@/lib/api";

// What it writes and sends as you, from your own mailbox. One setting on the
// server (PUT /delegation/reach), shown as two pickers: What it writes as
// you on Delegate → Act as me (Off, When I ask, Draft replies automatically) and What it
// sends on its own on Delegate → Handle it for me (Off or On). What goes on
// its own is the Approvals level's call (Settings → Your Executive), not a
// step of its own. Handle it for me builds on the inbox replies Act as me
// writes, so its switch opens once the first one is at Draft replies automatically.

// Where each picker sits, for links from the other tabs.
export const WRITES_ANCHOR = "writes";
export const SENDS_ANCHOR = "sends";
export const WRITES_HREF = `/delegate/act-as-me#${WRITES_ANCHOR}`;
export const SENDS_HREF = `/delegate/handle-it#${SENDS_ANCHOR}`;

export type WriteStep = "off" | "when_asked" | "inbox";
export type SendStep = "off" | "on";

// What Handle it for me sends at each Approvals level (delegation/handle_it.py,
// invites.py, action_cards.py).
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

export function reachOf(settings: DelegationSettings): ReachStep {
  if (!settings.enabled) return "off";
  if (!settings.inbox?.enabled) return "when_asked";
  if (!settings.handle_it?.enabled) return "inbox";
  return "handle_it";
}

// Act as me's picker: a sending step writes for the inbox too.
export function writeOf(settings: DelegationSettings): WriteStep {
  const step = reachOf(settings);
  return step === "off" || step === "when_asked" ? step : "inbox";
}

// Handle it for me's picker: off below the sending steps.
export function sendOf(settings: DelegationSettings): SendStep {
  const step = reachOf(settings);
  return step === "handle_it" ? "on" : "off";
}

// The reach step a pick on Handle it for me saves: Off keeps the inbox replies.
export function reachForSend(step: SendStep): ReachStep {
  return step === "off" ? "inbox" : "handle_it";
}

export function sentTodayLine(n: number): string {
  return n === 1 ? "Sent 1 reply on its own today." : `Sent ${n} replies on its own today.`;
}
