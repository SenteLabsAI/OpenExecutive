// The "Who writes and who sends" card on Delegate → What it may do: one
// short scene per option of What it writes as you (Act as me), What it sends
// on its own (Handle it for me) and Take the lead. Tapping an option plays
// its scene; it changes no setting. Pure data; a test holds the option
// labels to the ones the settings cards use (lib/reach.ts).

import type { SendStep, WriteStep } from "@/lib/reach";

export type Tab = "act" | "handle" | "lead";
export type LeadStep = "off" | "training" | "on";
export type Spot = "inbox" | "exec" | "you" | "to";

export type Chip = { at: Spot; from: string; text: string; tint?: boolean };

// One frame is the whole picture at that moment, so any frame can be shown
// on its own (reduced motion shows the last one).
export type Frame = {
  chip?: Chip;
  on?: Spot[];
  dim?: Spot[];
  caps?: Partial<Record<Spot, string>>;
  youSays?: string;
  execSees?: string;
  to?: string;
  sent?: string;
};

export type Scene = {
  key: string;
  tab: Tab;
  option: string;
  line: string;
  writes: WriteStep;
  sends: SendStep;
  lead: LeadStep;
  frames: Frame[];
};

export const WRITE_OPTIONS: { value: WriteStep; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "when_asked", label: "When I ask" },
  { value: "inbox", label: "For my inbox too" },
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

const mail = (from: string, text: string, at: Spot = "inbox", tint = false): Chip => ({ from, text, at, tint });

// Mail it sends on its own: in, written, out, then listed in What it sent.
function goes(who: string, asked: string, reply: string, to: string): Frame[] {
  return [
    { chip: mail(`From ${who}`, asked), on: ["inbox"], to },
    { chip: mail("Reply as you", reply, "exec", true), on: ["exec"], dim: ["you"], caps: { exec: "Sends it on its own" }, to },
    { chip: mail("From you", reply, "to", true), on: ["to"], dim: ["you"], caps: { to: "Gets it" }, to },
    { on: ["you"], sent: `Replied to ${to} as you`, to },
  ];
}

// Mail that waits: in, drafted, then on Home for you to send.
function waits(who: string, asked: string, reply: string, why: string, to: string): Frame[] {
  return [
    { chip: mail(`From ${who}`, asked), on: ["inbox"], to },
    { chip: mail("Draft as you", reply, "exec", true), on: ["exec"], caps: { exec: why }, to },
    { chip: mail("Draft as you", reply, "you", true), on: ["you"], caps: { you: "On Home" }, youSays: "Waits for you", to },
  ];
}

const late = "Sam's Q3 numbers are 3 days late";
const ask = "Hi Sam, can you send the Q3 numbers?";

export const SCENES: Scene[] = [
  {
    key: "writes:off", tab: "act", option: "Off", writes: "off", sends: "off", lead: "off",
    line: "The Executive only writes as itself. You answer your own mail.",
    frames: [
      { chip: mail("From Dana", "Can we move to the 5th?"), on: ["inbox"] },
      { chip: mail("From Dana", "Can we move to the 5th?", "you"), on: ["you"], dim: ["exec"], youSays: "You write the reply" },
      { chip: mail("From you", "Yes, the 5th works.", "to"), on: ["to"], dim: ["exec"], caps: { to: "Gets it" } },
    ],
  },
  {
    key: "writes:when_asked", tab: "act", option: "When I ask", writes: "when_asked", sends: "off", lead: "off",
    line: "Ask in chat, “reply to Dana as me: yes to the 5th”, and the draft waits in your Drafts.",
    frames: [
      { on: ["you"], dim: ["inbox"], youSays: "“Reply to Dana as me: yes to the 5th”" },
      { chip: mail("Draft as you", "Yes, the 5th works.", "exec", true), on: ["exec"], dim: ["inbox"], caps: { exec: "Writes it in your voice" } },
      { chip: mail("Draft as you", "Yes, the 5th works.", "you", true), on: ["you"], dim: ["inbox"], caps: { you: "In your Drafts" }, youSays: "You tap Send" },
      { chip: mail("From you", "Yes, the 5th works.", "to", true), on: ["to"], dim: ["inbox"], caps: { to: "Gets it" } },
    ],
  },
  {
    key: "writes:inbox", tab: "act", option: "For my inbox too", writes: "inbox", sends: "off", lead: "off",
    line: "It also writes a first reply to mail that needs you and puts it on Home. You tap Send.",
    frames: [
      { chip: mail("From Dana", "Can we move to the 5th?"), on: ["inbox"] },
      { chip: mail("Draft as you", "Yes, the 5th works.", "exec", true), on: ["exec"], caps: { exec: "Writes a draft" } },
      { chip: mail("Draft as you", "Yes, the 5th works.", "you", true), on: ["you"], caps: { you: "On Home" }, youSays: "You tap Send" },
      { chip: mail("From you", "Yes, the 5th works.", "to", true), on: ["to"], caps: { to: "Gets it" } },
    ],
  },
  {
    key: "sends:off", tab: "handle", option: "Off", writes: "inbox", sends: "off", lead: "off",
    line: "Every reply waits for you to send.",
    frames: waits("Dana", "Thanks. See you Tuesday?", "Yes, see you Tuesday.", "Writes a draft", "Dana"),
  },
  {
    key: "sends:careful", tab: "handle", option: "Easy ones", writes: "inbox", sends: "careful", lead: "off",
    line: "Only short replies to people you know, when it's very sure.",
    frames: [
      ...goes("Dana, a contact", "Thanks. See you Tuesday?", "Yes, see you Tuesday.", "Dana"),
      ...waits("Lee, a contact", "Can you send the deck and next year's price?", "Here's the deck. The price is…", "Longer reply: waits", "Lee"),
    ],
  },
  {
    key: "sends:balanced", tab: "handle", option: "People I know", writes: "inbox", sends: "balanced", lead: "off",
    line: "Replies to people you know. Strangers, links and amounts wait for you.",
    frames: [
      ...goes("Lee, a contact", "Can we push the review to Friday?", "Friday works. I'll move the invite.", "Lee"),
      ...waits("someone new", "Hi, are you the right person for partnerships?", "Yes, happy to talk.", "New sender: waits", "Alex"),
    ],
  },
  {
    key: "sends:bold", tab: "handle", option: "Most mail", writes: "inbox", sends: "bold", lead: "off",
    line: "Also strangers and longer replies, and links or amounts when it's very sure.",
    frames: [
      ...goes("someone new", "Hi, are you the right person for partnerships?", "Yes, happy to talk. Does Thursday suit?", "Alex"),
      ...waits("Lee, a contact", "Can you sign the updated contract?", "I'll review it and get back to you.", "Contracts always wait", "Lee"),
    ],
  },
  {
    key: "sends:everything", tab: "handle", option: "Everything", writes: "inbox", sends: "everything", lead: "off",
    line: "It decides what to send as you. Replies with a link, the topics that always wait and your rules still hold it back.",
    frames: [
      ...goes("someone new", "Could you share your Q3 plan?", "Here's a summary of the plan.", "Alex"),
      ...waits("Lee, a contact", "Where can I download the files?", "Here's the link: …", "Links always wait", "Lee"),
    ],
  },
  {
    key: "lead:off", tab: "lead", option: "Off", writes: "inbox", sends: "off", lead: "off",
    line: "It doesn't act in its own name unless you ask.",
    frames: [
      { on: ["exec"], dim: ["inbox"], caps: { exec: "Notices something" }, execSees: late, to: "Sam" },
      { on: ["you"], dim: ["inbox", "to"], caps: { you: "You decide what to do" }, to: "Sam" },
    ],
  },
  {
    key: "lead:training", tab: "lead", option: "In training", writes: "inbox", sends: "off", lead: "training",
    line: "It acts in its own name, and people can see it's the Executive. In training, each action waits for your yes on Home.",
    frames: [
      { on: ["exec"], dim: ["inbox"], caps: { exec: "Notices something" }, execSees: late, to: "Sam" },
      { chip: mail("From the Executive", ask, "exec", true), on: ["exec"], dim: ["inbox"], caps: { exec: "Writes in its own name" }, to: "Sam" },
      { chip: mail("From the Executive", ask, "you", true), on: ["you"], dim: ["inbox"], caps: { you: "On Home" }, youSays: "Waits for your yes", to: "Sam" },
      { chip: mail("From the Executive", ask, "to", true), on: ["to"], dim: ["inbox"], caps: { to: "Gets it" }, to: "Sam" },
    ],
  },
  {
    key: "lead:on", tab: "lead", option: "On", writes: "inbox", sends: "off", lead: "on",
    line: "It acts in its own name without waiting to be asked. Always asks first and your company's rules still hold.",
    frames: [
      { on: ["exec"], dim: ["inbox"], caps: { exec: "Notices something" }, execSees: late, to: "Sam" },
      { chip: mail("From the Executive", ask, "exec", true), on: ["exec"], dim: ["inbox", "you"], caps: { exec: "Writes in its own name" }, to: "Sam" },
      { chip: mail("From the Executive", ask, "to", true), on: ["to"], dim: ["inbox", "you"], caps: { to: "Gets it" }, to: "Sam" },
    ],
  },
];

export const sceneKey = (group: "writes" | "sends" | "lead", value: string) => `${group}:${value}`;

export function sceneFor(key: string): Scene {
  return SCENES.find((s) => s.key === key) ?? SCENES[0];
}

// Played in turn when nothing has been tapped: one scene per tab.
export const TOUR = ["writes:inbox", "sends:balanced", "lead:training"];

export function nextInTour(key: string): string {
  const i = TOUR.indexOf(key);
  return TOUR[(i + 1) % TOUR.length];
}
