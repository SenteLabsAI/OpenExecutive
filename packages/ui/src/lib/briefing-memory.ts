// Most briefing handoff seeds quote the Executive's own card (an artifact's
// body, a proposal, a monitoring signal). Peer memory records a turn's user
// message as the user's own words, so recording the seed would credit the
// user with what the Executive wrote ("<user> assigned a colleague to …"). Those
// handoffs send this short line as the turn's memory text instead.
//
// Phrase `action` as the user talking to the Executive, in the first person
// or the imperative ("Let's discuss …", "I approve …"). Honcho reads the line
// as the user's own message, and a clipped past participle ("Asked to …")
// comes back as a passive fact — "<user> was asked to …".
//
// The action is its own sentence, ended with "." before the subject on the
// next line. Episodic extraction and open loops only accept a commitment
// quoted from this text and followed by "." — a headline such as "Should we
// renew?" placed inside the sentence would put a "?" first and void it.

// Well under the backend's 2000-char `ChatRequest.memory_text` bound, so a
// long narrative bullet can never 422 the whole turn.
export const MEMORY_LINE_MAX_CHARS = 300;

// By code point, not UTF-16 unit: a cut through an emoji leaves a lone
// surrogate, which the backend cannot store.
function clip(text: string, max: number): string {
  const chars = Array.from(text);
  return chars.length > max ? `${chars.slice(0, max - 1).join("")}…` : text;
}

export function briefingMemoryLine(action: string, subject: string): string {
  const s = clip(subject.trim().replace(/\s+/g, " "), MEMORY_LINE_MAX_CHARS);
  return s ? `${action}.\n"${s}"` : `${action}.`;
}

// The handoff actions, in the phrasing the comment above asks for. Kept here
// rather than inline so the wording is tested.
export const MEMORY_ACTIONS = {
  artifact: "Let's discuss this flagged artifact",
  monitoring: "Help me understand this monitored signal",
  meeting: "Let's talk through this proposed meeting",
  proposal: "Tell me about this proposal",
  narrative: "Let's dig into this briefing item",
  approve: "I approve this proposal",
  // The edited text itself is left out: it starts as the Executive's card
  // body and is often sent unchanged or lightly edited, so recording it would
  // let the Executive's recommendation pass as the user's own decision.
  approveWithEdits: "I approve this proposal with my edits",
} as const;

export function nudgeAction(who: string): string {
  return `Nudge ${who} about this item`;
}
