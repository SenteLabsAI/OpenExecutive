// What the chat's last turn allows: Try again on the last reply, Edit on the
// last message the user sent. Both take the turn back on the server
// (POST /sessions/{id}/rewind) and send the words again. Kept apart from the
// component so `npm test` can check it (see scripts/chatTurns.test.mjs).

import type { ChatMessage } from "@/lib/api";

export interface LastTurn {
  /** Index of the last message when it is a saved assistant reply. */
  replyIndex: number | null;
  /** Index of the last user message before that reply. */
  userIndex: number | null;
  /** The reply's row id, which the rewind names. */
  replyId: number | null;
  /** Whether Try again / Edit may be offered at all. */
  canRedo: boolean;
  /** The reply already acted (sent, booked, filed...), so redoing could repeat it. */
  didSomething: boolean;
}

/** Slack, Telegram, Discord and Google Chat chats have ids like
 * "slack:dm:..."; the reply also lives in that app and can't be taken back. */
function isChannelSession(sessionId: string): boolean {
  return sessionId.includes(":");
}

export function lastTurn(messages: ChatMessage[], sessionId: string | null | undefined): LastTurn {
  const last = messages.length - 1;
  const reply = last >= 0 ? messages[last] : undefined;
  const replyIndex = reply && reply.role === "assistant" && typeof reply.id === "number" ? last : null;
  let userIndex: number | null = null;
  if (replyIndex !== null) {
    for (let i = replyIndex - 1; i >= 0; i--) {
      if (messages[i].role === "user") {
        userIndex = i;
        break;
      }
      // Another reply first: this one answered nothing the user typed.
      if (messages[i].role === "assistant") break;
    }
  }
  const replyId = replyIndex !== null ? (reply?.id ?? null) : null;
  return {
    replyIndex,
    userIndex,
    replyId,
    canRedo: !!sessionId && !isChannelSession(sessionId) && replyId !== null,
    didSomething: replyIndex !== null && (reply?.actions?.length ?? 0) > 0,
  };
}

/** The messages without the last turn: the last reply and the user messages
 * it answered (everything after the reply before it). */
export function dropLastTurn(messages: ChatMessage[]): ChatMessage[] {
  let end = messages.length;
  // Skip the trailing reply (if the list ends on one), then the user
  // messages before it, stopping at the previous reply.
  if (end > 0 && messages[end - 1].role === "assistant") end -= 1;
  while (end > 0 && messages[end - 1].role === "user") end -= 1;
  return messages.slice(0, end);
}

/** The question asked before redoing a reply that already did something. */
export function confirmRedoText(actions: { summary: string }[] | undefined): string {
  const first = actions?.find((a) => a.summary.trim())?.summary.trim();
  const what = first ? `("${first}")` : "(sent a message, booked a meeting or filed a card)";
  return `That answer already did something ${what}. Doing it again could repeat it. Go ahead?`;
}
