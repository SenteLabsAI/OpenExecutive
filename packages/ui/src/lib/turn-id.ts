// Client-minted id for one chat turn. The client needs an id *before* the
// request goes out: the backend spends several seconds fetching context
// (RAG + episodic + Honcho + briefing) before the SSE response even exists,
// so a server-issued id would leave the Stop button dead during exactly the
// window where stopping is free — no model call has been made yet.
//
// Paired constant: `_CLIENT_TURN_ID_RE` in
// packages/core/openexecutive/api/routes/chat.py. Keep the two in sync — the
// server drops an id that doesn't match, which silently makes turns
// unstoppable. The charset is narrow because the value reaches server log
// lines.
export const CLIENT_TURN_ID_RE = /^[A-Za-z0-9-]{8,64}$/;

export function newClientTurnId(): string {
  // `crypto.randomUUID` needs a secure context; a plain-HTTP LAN dev host
  // doesn't have one, and a stop button that throws is worse than one that
  // falls back to a slightly shorter random id.
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  if (typeof c?.getRandomValues === "function") {
    const bytes = c.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  // Last resort. Not unguessable, but the id only addresses this browser's own
  // in-flight turn, and the server also checks the caller owns it.
  return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
