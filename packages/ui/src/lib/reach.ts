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
export const APPROVALS_ANCHOR = "approvals";
// The Approvals tab's earlier address, still opened by older links.
const OLD_APPROVALS_ANCHOR = "before-anything-goes-out";
export const AS_YOU_HREF = `/delegate#${AS_YOU_ANCHOR}`;
export const AS_ME_HREF = `/delegate#${AS_ME_ANCHOR}`;
export const APPROVALS_HREF = `/delegate#${APPROVALS_ANCHOR}`;

export const REACH_ORDER: ReachStep[] = ["off", "when_asked", "inbox"];

// Short names for the As you tab's badge, the same as the picker's choices.
export const REACH_LABEL: Record<ReachStep, string> = { off: "Off", when_asked: "When you ask", inbox: "Your inbox" };

// The page's three tabs, named by their anchors so the old links still land
// on the right one.
export type DelegateTab = typeof AS_YOU_ANCHOR | typeof AS_ME_ANCHOR | typeof APPROVALS_ANCHOR;
export const DELEGATE_TABS: DelegateTab[] = [AS_YOU_ANCHOR, AS_ME_ANCHOR, APPROVALS_ANCHOR];

// Which tab a link's #hash opens. Anything else opens As you.
export function tabFromHash(hash: string): DelegateTab {
  const raw = hash.replace(/^#/, "");
  const id = raw === OLD_APPROVALS_ANCHOR ? APPROVALS_ANCHOR : raw;
  return (DELEGATE_TABS as string[]).includes(id) ? (id as DelegateTab) : AS_YOU_ANCHOR;
}

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
    line: "Replies and follow-ups go when it's sure and they leave nothing for you to answer, to the people the email was already with. Invites and suggested actions go for your team and contacts.",
  },
  trust: {
    label: "Hands-off",
    line: "Replies and follow-ups go when it's sure and they leave nothing for you to answer, to the people the email was already with. Invites from people you've written to and suggested actions for anyone go too.",
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

// The As you tab's badge: the step, and "In training" beside it while that
// is on and it writes as you at all, so it reads from the tab row.
export function asYouBadge(settings: DelegationSettings): string {
  const step = reachOf(settings);
  return step !== "off" && settings.training?.training ? `${REACH_LABEL[step]} · In training` : REACH_LABEL[step];
}

// The "Now:" line under the As you picker.
export function reachLine(step: ReachStep, inTraining: boolean, level: OutsideContentMode | null): string {
  if (step === "off") return "It never writes as you.";
  if (step === "when_asked") return "Ask in chat and the draft waits in your Drafts. You send it.";
  if (inTraining) return "It drafts replies to your inbox. In training, every one waits for you until you allow that person.";
  if (level === "ask" || level === null) return "It drafts replies to your inbox. On Careful, every one waits for you.";
  return `On ${APPROVALS_SENDS[level].label} it sends a reply itself when it is sure and the reply leaves nothing for you to answer. The rest wait on Home.`;
}

export function sentTodayLine(n: number): string {
  return n === 1 ? "Sent 1 reply on its own today." : `Sent ${n} replies on its own today.`;
}

// The tab a click on a link opens on this page, or null when it leaves the
// page alone: a click that opens a new tab or window (a modifier key, a
// button other than the left one, a target other than this one), or a link
// to somewhere other than Delegate. A plain /delegate link opens As you.
export function tabForClick(click: {
  button: number;
  modified: boolean;
  target: string;
  href: string;
  origin: string;
}): DelegateTab | null {
  if (click.button !== 0 || click.modified) return null;
  if (click.target && click.target !== "_self") return null;
  const url = new URL(click.href, click.origin);
  if (url.origin !== click.origin || url.pathname !== "/delegate") return null;
  return tabFromHash(url.hash);
}
