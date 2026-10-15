import type { DelegationSettings, HandleItMode, ReachStep } from "@/lib/api";

// How far it goes as you: the one dial for everything Act as me does from
// your own mailbox (PUT /delegation/reach). It lives on Delegate → Act as me
// only; Handle it for me and What it may do show where it is and link here.

// Where the dial sits on Act as me, for links from the other tabs.
export const REACH_HREF = "/delegate/act-as-me#how-far";
export const REACH_ANCHOR = "how-far";

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

export function stepLabel(step: ReachStep): string {
  if (step === "off") return "Off";
  if (step === "when_asked") return "Drafts when I ask";
  if (step === "inbox") return "Drafts for my inbox";
  if (step === "everything") return "Everything";
  return HANDLE_IT_MODES.find((m) => m.mode === step)?.label ?? step;
}

// Where the dial is, for the Handle it for me tab: the step, whether that
// step sends, whether it's in training, and one line on what it means.
export function reachSummary(settings: DelegationSettings): {
  label: string;
  sends: boolean;
  training: boolean;
  line: string;
} {
  const step = reachOf(settings);
  const mode = HANDLE_IT_MODES.find((m) => m.mode === step);
  const sends = Boolean(mode) || step === "everything";
  const training = sends && step !== "everything" && Boolean(settings.training?.replies || settings.training?.follow_ups);
  const line = mode
    ? mode.replies
    : step === "everything"
      ? LEAD_LINE
      : "Handle it for me is off. Every reply waits for you to tap Send.";
  return { label: stepLabel(step), sends, training, line };
}

export function sentTodayLine(n: number): string {
  return n === 1 ? "Sent 1 reply on its own today." : `Sent ${n} replies on its own today.`;
}
