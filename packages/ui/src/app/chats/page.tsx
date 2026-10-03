"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Icon from "@/components/Icon";
import { buttonClass } from "@/components/ui/Button";
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

// Full conversation history: every chat, including conversations that
// arrived through Slack, Telegram and Discord. Each row opens its chat;
// Delete is in the row's ⋯ menu and asks first.
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
          <div className="mb-6 flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg mb-1">Chats</h1>
              <p className="text-[15px] text-fg-muted">
                Every conversation with the Executive, from the web app and from connected
                channels.
              </p>
            </div>
            <Link href="/?new=1" className={buttonClass("primary", "md", "flex-shrink-0")}>
              <Icon name="plus" size="w-4 h-4" />
              <span className="hidden sm:inline">New chat</span>
              <span className="sm:hidden">New</span>
            </Link>
          </div>

          <div className="flex flex-col sm:flex-row sm:items-center gap-3 mb-5">
            <div
              role="tablist"
              aria-label="Filter by channel"
              className="inline-flex flex-wrap items-center gap-1 p-1 rounded-xl ring-1 ring-line bg-surface-elevated"
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
                    className={`min-h-[40px] px-4 text-[15px] font-medium rounded-lg transition cursor-pointer ${
                      active ? "bg-accent/10 text-accent" : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
                    }`}
                  >
                    {t === "all" ? "All" : CHANNEL_LABELS[t]}
                    <span className="ml-1.5 text-fg-subtle">{count}</span>
                  </button>
                );
              })}
            </div>
            <div className="relative sm:ml-auto sm:w-72">
              <Icon
                name="search"
                size="w-4 h-4"
                className="absolute left-3.5 top-1/2 -translate-y-1/2 text-fg-subtle pointer-events-none"
              />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search conversations"
                aria-label="Search conversations"
                className="w-full h-11 rounded-xl bg-surface-elevated border border-line pl-10 pr-3 text-[15px] text-fg placeholder:text-fg-subtle focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20"
              />
            </div>
          </div>

          {!loaded ? (
            <p className="px-1 py-6 text-[15px] text-fg-muted">Loading conversations…</p>
          ) : error && sessions.length === 0 ? (
            <p className="px-1 py-6 text-[15px] text-fg-muted">
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
            <p className="px-1 py-6 text-[15px] text-fg-muted">
              No conversations yet. Start one with New chat.
            </p>
          ) : (
            <ChatHistory
              sessions={visible}
              searching={searching}
              onSelect={(id) => router.push(`/?session=${encodeURIComponent(id)}`)}
              onDelete={remove}
            />
          )}
        </div>
      </main>
    </div>
  );
}
