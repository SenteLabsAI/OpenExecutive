// The "Watch how" card in What it does for you: one short scene per step of
// How far it goes as you, per Approvals level for what goes on its own from
// your inbox, and per Approvals level for what it does as itself. Tapping
// an option plays its scene; it changes no setting. Pure data; a test holds
// the option labels to the ones the Delegate page uses (lib/reach.ts).

import type { ReachStep } from "@/lib/api";

export type Tab = "act" | "handle" | "lead";
// The Approvals levels (Settings → Your Executive).
export type LevelStep = "careful" | "balanced" | "hands_off";
export type Who = "you" | "exec" | "them";

// One numbered step of a scene. Steps appear in turn and stay on screen.
export type Step = {
  who: Who;
  name: string;
  does: string;
  mail?: { from: string; text: string; as?: "you" | "exec" };
};

// The three answers above every scene.
export type Answers = { writes: string; name: string; sends: string };

export type Scene = {
  key: string;
  tab: Tab;
  option: string;
  line: string;
  answers: Answers;
  // Act as me and Take the lead tell a story in steps. Handle it for me
  // sorts the same emails (MAILS) every time: the first `sends` of them go
  // out on their own, the rest wait.
  steps?: Step[];
  sends?: number;
};

export const WRITE_OPTIONS: { value: ReachStep; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "when_asked", label: "When you ask" },
  { value: "inbox", label: "Your inbox" },
];

export const LEVEL_OPTIONS: { value: LevelStep; label: string }[] = [
  { value: "careful", label: "Careful" },
  { value: "balanced", label: "Balanced" },
  { value: "hands_off", label: "Hands-off" },
];

// What goes on its own from your inbox, at each Approvals level: no switch
// of its own.
export const SEND_OPTIONS: { value: LevelStep; label: string }[] = LEVEL_OPTIONS;

// As me has no switch either: Approvals decides each step.
export const LEAD_OPTIONS = LEVEL_OPTIONS;

export const TAB_NAMES: Record<Tab, string> = {
  act: "As you writes",
  handle: "As you sends",
  lead: "As me",
};

export const OPTIONS: Record<Tab, { value: string; label: string }[]> = {
  act: WRITE_OPTIONS,
  handle: SEND_OPTIONS,
  lead: LEAD_OPTIONS,
};

// The option a tab opens on.
export const FIRST: Record<Tab, string> = { act: "when_asked", handle: "balanced", lead: "balanced" };

// As you sends: the same five emails at every level, so the levels
// compare. Ordered from the easiest to send to the one that always waits.
export const MAILS: { from: string; text: string; why: string }[] = [
  { from: "Dana, a contact", text: "See you Tuesday?", why: "Short reply" },
  { from: "Lee, a contact", text: "Can you send a summary of where the project stands?", why: "Longer reply" },
  { from: "Alex, someone new", text: "Are you the right person for partnerships?", why: "New sender" },
  { from: "Pat, you wrote to them once", text: "Invite: Pilot review, Wed 14:30", why: "Invite, not a contact" },
  { from: "Kim, a contact", text: "Can you loop in your finance lead?", why: "New recipient always waits" },
];

const dana: Step = { who: "them", name: "Dana", does: "Emails you", mail: { from: "From Dana", text: "Can we move to the 5th?" } };
const yes = "Yes, the 5th works.";
const notices: Step = { who: "exec", name: "The Executive", does: "Notices Sam's Q3 numbers are 3 days late" };
const writesToSam: Step = {
  who: "exec",
  name: "The Executive",
  does: "Writes to Sam in its own name",
  mail: { from: "From the Executive", text: "Hi Sam, can you send the Q3 numbers?", as: "exec" },
};
const yours = (writes: string): Answers => ({ writes, name: "Yours", sends: "You" });

export const SCENES: Scene[] = [
  {
    key: "writes:off", tab: "act", option: "Off", answers: yours("You"),
    line: "The Executive doesn't write as you. You answer your own mail.",
    steps: [
      dana,
      { who: "you", name: "You", does: "Write the reply yourself" },
      { who: "you", name: "You", does: "Tap Send", mail: { from: "From you", text: yes, as: "you" } },
    ],
  },
  {
    key: "writes:when_asked", tab: "act", option: "When you ask", answers: yours("The Executive"),
    line: "It writes as you only when you ask in chat.",
    steps: [
      dana,
      { who: "you", name: "You", does: "Ask in chat", mail: { from: "You, in chat", text: "Reply to Dana as me: yes to the 5th" } },
      { who: "exec", name: "The Executive", does: "Writes the reply in your voice and puts it in your Drafts", mail: { from: "Draft, from you", text: yes, as: "you" } },
      { who: "you", name: "You", does: "Check it and tap Send. Dana gets it from you." },
    ],
  },
  {
    key: "writes:inbox", tab: "act", option: "Your inbox", answers: yours("The Executive"),
    line: "It also drafts replies to mail that needs you, without being asked. Approvals decides which go on their own.",
    steps: [
      dana,
      { who: "exec", name: "The Executive", does: "Writes a reply in your voice", mail: { from: "Draft, from you", text: yes, as: "you" } },
      { who: "you", name: "You", does: "See the draft on Home" },
      { who: "you", name: "You", does: "Tap Send. Dana gets it from you." },
    ],
  },
  {
    key: "sends:careful", tab: "handle", option: "Careful", answers: yours("The Executive"), sends: 0,
    line: "Approvals is on Careful: every reply waits on Home for you to send. In training holds them all too, on any level.",
  },
  {
    key: "sends:balanced", tab: "handle", option: "Balanced", sends: 3,
    answers: { writes: "The Executive", name: "Yours", sends: "It or you" },
    line: "Replies go when it's sure, to the people the email was already with. Invites go for your team and contacts.",
  },
  {
    key: "sends:hands_off", tab: "handle", option: "Hands-off", sends: 4,
    answers: { writes: "The Executive", name: "Yours", sends: "It or you" },
    line: "The same, and invites from people you've written to go too. A new recipient still waits.",
  },
  {
    key: "lead:careful", tab: "lead", option: "Careful",
    answers: { writes: "The Executive", name: "The Executive's", sends: "You, with Approve on Home" },
    line: "It writes in its own name, and each message waits for you to approve it.",
    steps: [notices, writesToSam, { who: "you", name: "You", does: "Tap Approve on Home" }, { who: "them", name: "Sam", does: "Gets it from the Executive" }],
  },
  {
    key: "lead:balanced", tab: "lead", option: "Balanced",
    answers: { writes: "The Executive", name: "The Executive's", sends: "The Executive, or you for the kinds that wait" },
    line: "A plain message to your team or a contact goes on its own. Money, contracts, people decisions, someone new, deleting or sharing, and big sends wait for your Approve.",
    steps: [notices, writesToSam, { who: "them", name: "Sam", does: "Gets it from the Executive" }],
  },
  {
    key: "lead:hands_off", tab: "lead", option: "Hands-off",
    answers: { writes: "The Executive", name: "The Executive's", sends: "The Executive" },
    line: "It acts in its own name without waiting for you.",
    steps: [notices, writesToSam, { who: "them", name: "Sam", does: "Gets it from the Executive" }],
  },
];

const GROUP: Record<Tab, "writes" | "sends" | "lead"> = { act: "writes", handle: "sends", lead: "lead" };

export const sceneKey = (tab: Tab, value: string) => `${GROUP[tab]}:${value}`;

export function sceneFor(key: string): Scene {
  return SCENES.find((s) => s.key === key) ?? SCENES.find((s) => s.key === sceneKey("act", FIRST.act))!;
}

// How many beats a scene plays: one per step, or one per email.
export function beats(scene: Scene): number {
  return scene.steps ? scene.steps.length : MAILS.length;
}
