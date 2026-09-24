"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Icon from "@/components/Icon";
import ChatHistory from "@/components/sessions/ChatHistory";
import { useSessions } from "@/components/sessions/SessionsContext";
import {
  CHANNEL_LABELS,
  CHANNEL_ORDER,
  countByChannel,
  filterSessions,
  type SessionChannel,
} from "@/lib/sessionChannel";

type ChannelFilter = SessionChannel | "all";

// Full conversation history. The sidebar only lists the latest few web chats;
// this page holds everything, including conversations that arrived through
// Slack, Telegram and Discord.
export default function ChatsPage() {
  const router = useRouter();
  const { sessions, loaded, error, refresh, remove } = useSessions();
  const [channel, setChannel] = useState<ChannelFilter>("all");
  const [query, setQuery] = useState("");

  useEffect(() => {
    refresh();
  }, [refresh]);

  const counts = useMemo(() => countByChannel(sessions), [sessions]);
  // "All" and "Web" always show; a channel tab appears once it has a chat.
  const tabs: ChannelFilter[] = [
    "all",
    ...CHANNEL_ORDER.filter((c) => c === "web" || (counts[c] ?? 0) > 0),
  ];
  // Deleting a channel's last chat removes its tab; fall back to "All" rather
  // than leave an invisible filter selected over an empty list. The derived
  // value covers this render; the effect makes it stick, so a later refresh
  // that brings the channel back doesn't silently re-select it.
  const channelShown = tabs.includes(channel);
  const activeChannel: ChannelFilter = channelShown ? channel : "all";
  useEffect(() => {
    if (!channelShown) setChannel("all");
  }, [channelShown]);
  const visible = useMemo(
    () => filterSessions(sessions, { channel: activeChannel, query }),
    [sessions, activeChannel, query],
  );
  const searching = query.trim().length > 0;

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-4 sm:px-6 py-8">
        <div className="max-w-3xl mx-auto">
          <div className="mb-6">
            <h1 className="text-2xl font-semibold text-fg mb-1">Chats</h1>
            <p className="text-sm text-fg-muted">
              Every conversation with the Executive, from the web app and from connected
              channels. The sidebar shows only your latest web chats.
            </p>
          </div>

          <div className="flex flex-col sm:flex-row sm:items-center gap-3 mb-5">
            <div
              role="tablist"
              aria-label="Filter by channel"
              className="inline-flex flex-wrap items-center gap-1 p-0.5 rounded-lg ring-1 ring-line bg-surface/40"
            >
              {tabs.map((t) => {
                const active = activeChannel === t;
                const count = t === "all" ? sessions.length : (counts[t] ?? 0);
                return (
                  <button
                    key={t}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => setChannel(t)}
                    className={`px-3 py-1.5 text-xs rounded-md transition cursor-pointer ${
                      active ? "bg-surface-elevated text-fg" : "text-fg-muted hover:text-fg"
                    }`}
                  >
                    {t === "all" ? "All" : CHANNEL_LABELS[t]}
                    <span className="ml-1.5 text-fg-subtle">{count}</span>
                  </button>
                );
              })}
            </div>
            <div className="relative sm:ml-auto sm:w-64">
              <Icon
                name="search"
                size="w-3.5 h-3.5"
                className="absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-subtle pointer-events-none"
              />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search conversations"
                aria-label="Search conversations"
                className="w-full rounded-lg bg-surface-input/60 border border-line pl-8 pr-2 py-2 text-sm text-fg placeholder:text-fg-subtle focus:outline-none focus:border-fg-subtle"
              />
            </div>
          </div>

          {!loaded ? (
            <p className="px-3 py-6 text-sm text-fg-muted">Loading conversations…</p>
          ) : error && sessions.length === 0 ? (
            <p className="px-3 py-6 text-sm text-fg-muted">
              Couldn&apos;t load your conversations.{" "}
              <button
                type="button"
                onClick={refresh}
                className="text-indigo-400 hover:text-indigo-300 font-medium cursor-pointer"
              >
                Try again
              </button>
            </p>
          ) : sessions.length === 0 ? (
            <p className="px-3 py-6 text-sm text-fg-muted">
              No conversations yet. Start one with New chat.
            </p>
          ) : (
            <ChatHistory
              sessions={visible}
              searching={searching}
              onSelect={(id) => router.push(`/?session=${encodeURIComponent(id)}`)}
              onDelete={(id) => void remove(id)}
            />
          )}
        </div>
      </main>
    </div>
  );
}
