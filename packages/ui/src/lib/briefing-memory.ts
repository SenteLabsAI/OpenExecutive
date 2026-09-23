// Most briefing handoff seeds quote the Executive's own card (an artifact's
// body, a proposal, a monitoring signal). Peer memory records a turn's user
// message as the user's own words, so recording the seed would credit the
// user with what the Executive wrote ("<user> assigned a colleague to …"). Those
// handoffs send this short line as the turn's memory text instead.

// Well under the backend's 2000-char `ChatRequest.memory_text` bound, so a
// long narrative bullet can never 422 the whole turn.
export const MEMORY_LINE_MAX_CHARS = 300;

export function briefingMemoryLine(action: string, subject: string): string {
  const s = subject.trim().replace(/\s+/g, " ");
  const clipped =
    s.length > MEMORY_LINE_MAX_CHARS ? `${s.slice(0, MEMORY_LINE_MAX_CHARS - 1)}…` : s;
  return clipped ? `${action} "${clipped}".` : `${action}.`;
}
