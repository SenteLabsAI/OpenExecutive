import type { SessionSummary } from "@/lib/api";

// Where a conversation came from. The backend has no channel column on
// `sessions`; the channel adapters namespace their session ids instead
// (`slack:dm:U1`, `telegram:123`, `discord:thread:…`), while web chats get a
// bare uuid. This classifies a session from that prefix alone.
export type SessionChannel = "web" | "slack" | "telegram" | "discord" | "email" | "google_chat";

export const CHANNEL_LABELS: Record<SessionChannel, string> = {
  web: "Web",
  slack: "Slack",
  telegram: "Telegram",
  discord: "Discord",
  email: "Email",
  google_chat: "Google Chat",
};

// Display order for the channel filter on /chats.
export const CHANNEL_ORDER: SessionChannel[] = [
  "web",
  "slack",
  "telegram",
  "discord",
  "email",
  "google_chat",
];

const PREFIXED_CHANNELS = new Set<SessionChannel>(
  CHANNEL_ORDER.filter((c) => c !== "web"),
);

export function sessionChannel(sessionId: string): SessionChannel {
  const sep = sessionId.indexOf(":");
  if (sep <= 0) return "web";
  const prefix = sessionId.slice(0, sep) as SessionChannel;
  return PREFIXED_CHANNELS.has(prefix) ? prefix : "web";
}

export function sessionTitle(session: SessionSummary): string {
  return session.title || "Untitled chat";
}

/** The newest `limit` web chats. Input is newest-first (the API sorts by `updated_at DESC`). */
export function recentWebChats(sessions: SessionSummary[], limit: number): SessionSummary[] {
  const out: SessionSummary[] = [];
  for (const s of sessions) {
    if (out.length >= limit) break;
    if (sessionChannel(s.session_id) === "web") out.push(s);
  }
  return out;
}

export function filterSessions(
  sessions: SessionSummary[],
  { channel, query }: { channel: SessionChannel | "all"; query: string },
): SessionSummary[] {
  const q = query.trim().toLowerCase();
  return sessions.filter(
    (s) =>
      (channel === "all" || sessionChannel(s.session_id) === channel) &&
      (q === "" || sessionTitle(s).toLowerCase().includes(q)),
  );
}

/** Session count per channel, for the filter tabs. Channels with no sessions are absent. */
export function countByChannel(sessions: SessionSummary[]): Partial<Record<SessionChannel, number>> {
  const counts: Partial<Record<SessionChannel, number>> = {};
  for (const s of sessions) {
    const c = sessionChannel(s.session_id);
    counts[c] = (counts[c] ?? 0) + 1;
  }
  return counts;
}
