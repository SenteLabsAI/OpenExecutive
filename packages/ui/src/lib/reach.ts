import type { DelegationSettings, HandleItMode, ReachStep } from "@/lib/api";

// What it writes and sends as you, from your own mailbox. One setting on the
// server (PUT /delegation/reach), shown as two pickers: What it writes as
// you on Delegate → Act as me (Off, When I ask, Draft replies automatically) and What it
// sends on its own on Delegate → Handle it for me (Off, then the sending
// steps). Handle it for me builds on the inbox replies Act as me writes, so
// its picker opens once the first one is at Draft replies automatically.

// Where each picker sits, for links from the other tabs.
export const WRITES_ANCHOR = "writes";
export const SENDS_ANCHOR = "sends";
export const WRITES_HREF = `/delegate/act-as-me#${WRITES_ANCHOR}`;
export const SENDS_HREF = `/delegate/handle-it#${SENDS_ANCHOR}`;

export type WriteStep = "off" | "when_asked" | "inbox";
export type SendStep = "off" | HandleItMode | "everything";

// The sending steps, which are Handle it for me.
export const HANDLE_IT_MODES: { mode: HandleItMode; label: string; replies: string; followUps: string }[] = [
  {
    mode: "careful",
    label: "Easy ones",
    replies: "Only short replies to people you know, when it's very sure.",
    followUps: "Written for you, and they wait on Today for you to send.",
  },
  {
    mode: "balanced",
    label: "People I know",
    replies: "Replies to people you know. Strangers, links and amounts wait for you.",
    followUps: "Sent to your team and contacts when they haven't answered a question of yours in a few days.",
  },
  {
    mode: "bold",
    label: "Most mail",
    replies: "Also strangers and longer replies, and links or amounts when it's very sure.",
    followUps: "Sent to anyone you wrote to who hasn't answered a question of yours in a few days.",
  },
];

export const LEAD_LINE =
  "It decides what to send as you. Replies with a link, the topics that always wait and your rules still hold it back.";

export function reachOf(settings: DelegationSettings): ReachStep {
  if (!settings.enabled) return "off";
  if (!settings.inbox?.enabled) return "when_asked";
  const h = settings.handle_it;
  if (!h?.enabled) return "inbox";
  if (h.lead) return "everything";
  return h.mode;
}

// Act as me's picker: a sending step writes for the inbox too.
export function writeOf(settings: DelegationSettings): WriteStep {
  const step = reachOf(settings);
  return step === "off" || step === "when_asked" ? step : "inbox";
}

// Handle it for me's picker: off below the sending steps.
export function sendOf(settings: DelegationSettings): SendStep {
  const step = reachOf(settings);
  return step === "off" || step === "when_asked" || step === "inbox" ? "off" : step;
}

// The reach step a pick on Handle it for me saves: Off keeps the inbox replies.
export function reachForSend(step: SendStep): ReachStep {
  return step === "off" ? "inbox" : step;
}

export function sentTodayLine(n: number): string {
  return n === 1 ? "Sent 1 reply on its own today." : `Sent ${n} replies on its own today.`;
}
