// The "Who writes and who sends" card on Delegate → What it may do: one
// short scene per option of What it writes as you (Act as me), What it sends
// on its own (Handle it for me) and Take the lead. Tapping an option plays
// its scene; it changes no setting. Pure data; a test holds the option
// labels to the ones the settings cards use (lib/reach.ts).

import type { SendStep, WriteStep } from "@/lib/reach";

export type Tab = "act" | "handle" | "lead";
export type LeadStep = "off" | "training" | "on";
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

export const WRITE_OPTIONS: { value: WriteStep; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "when_asked", label: "When I ask" },
  { value: "inbox", label: "Draft replies automatically" },
];

export const SEND_OPTIONS: { value: SendStep; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "careful", label: "Easy ones" },
  { value: "balanced", label: "People I know" },
  { value: "bold", label: "Most mail" },
  { value: "everything", label: "Everything" },
];

export const LEAD_OPTIONS: { value: LeadStep; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "training", label: "In training" },
  { value: "on", label: "On" },
];

export const TAB_NAMES: Record<Tab, string> = {
  act: "Act as me",
  handle: "Handle it for me",
  lead: "Take the lead",
};

export const OPTIONS: Record<Tab, { value: string; label: string }[]> = {
  act: WRITE_OPTIONS,
  handle: SEND_OPTIONS,
  lead: LEAD_OPTIONS,
};

// The option a tab opens on.
export const FIRST: Record<Tab, string> = { act: "when_asked", handle: "balanced", lead: "training" };

// Handle it for me: the same five emails at every level, so the levels
// compare. Ordered from the easiest to send to the one that always waits.
export const MAILS: { from: string; text: string; why: string }[] = [
  { from: "Dana, a contact", text: "See you Tuesday?", why: "Short reply" },
  { from: "Lee, a contact", text: "Can you send a summary of where the project stands?", why: "Longer reply" },
  { from: "Alex, someone new", text: "Are you the right person for partnerships?", why: "New sender" },
  { from: "Kim, a contact", text: "What would 20 seats cost?", why: "Has an amount" },
  { from: "Sam, a contact", text: "Can you sign the updated contract?", why: "Contracts always wait" },
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
    key: "writes:when_asked", tab: "act", option: "When I ask", answers: yours("The Executive"),
    line: "It writes as you only when you ask in chat.",
    steps: [
      dana,
      { who: "you", name: "You", does: "Ask in chat", mail: { from: "You, in chat", text: "Reply to Dana as me: yes to the 5th" } },
      { who: "exec", name: "The Executive", does: "Writes the reply in your voice and puts it in your Drafts", mail: { from: "Draft, from you", text: yes, as: "you" } },
      { who: "you", name: "You", does: "Check it and tap Send. Dana gets it from you." },
    ],
  },
  {
    key: "writes:inbox", tab: "act", option: "Draft replies automatically", answers: yours("The Executive"),
    line: "It also drafts replies to mail that needs you, without being asked.",
    steps: [
      dana,
      { who: "exec", name: "The Executive", does: "Writes a reply in your voice", mail: { from: "Draft, from you", text: yes, as: "you" } },
      { who: "you", name: "You", does: "See the draft on Home" },
      { who: "you", name: "You", does: "Tap Send. Dana gets it from you." },
    ],
  },
  {
    key: "sends:off", tab: "handle", option: "Off", answers: yours("The Executive"), sends: 0,
    line: "Every reply waits on Home for you to send.",
  },
  {
    key: "sends:careful", tab: "handle", option: "Easy ones", sends: 1,
    answers: { writes: "The Executive", name: "Yours", sends: "It or you" },
    line: "Only short replies to people you know, when it's very sure.",
  },
  {
    key: "sends:balanced", tab: "handle", option: "People I know", sends: 2,
    answers: { writes: "The Executive", name: "Yours", sends: "It or you" },
    line: "Replies to people you know, short or long. Strangers and amounts wait for you.",
  },
  {
    key: "sends:bold", tab: "handle", option: "Most mail", sends: 3,
    answers: { writes: "The Executive", name: "Yours", sends: "It or you" },
    line: "Also people you don't know. Amounts and links only when it's very sure.",
  },
  {
    key: "sends:everything", tab: "handle", option: "Everything", sends: 4,
    answers: { writes: "The Executive", name: "Yours", sends: "It or you" },
    line: "It decides what to send as you. Links, the topics that always wait and your rules still hold it back.",
  },
  {
    key: "lead:off", tab: "lead", option: "Off", answers: { writes: "You", name: "Yours", sends: "You" },
    line: "It doesn't act in its own name. It tells you and you decide.",
    steps: [notices, { who: "exec", name: "The Executive", does: "Tells you on Home" }, { who: "you", name: "You", does: "Decide what to do" }],
  },
  {
    key: "lead:training", tab: "lead", option: "In training",
    answers: { writes: "The Executive", name: "The Executive's", sends: "You, with a yes on Home" },
    line: "It writes in its own name, and each message waits for your yes.",
    steps: [notices, writesToSam, { who: "you", name: "You", does: "Tap Yes on Home" }, { who: "them", name: "Sam", does: "Gets it from the Executive" }],
  },
  {
    key: "lead:on", tab: "lead", option: "On",
    answers: { writes: "The Executive", name: "The Executive's", sends: "The Executive" },
    line: "It acts in its own name without waiting for your yes on Home. Your company's rules and the Always asks first list still hold it back.",
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
