"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Icon from "@/components/Icon";
import { buttonClass } from "@/components/ui/Button";
import ChatHistory, { ChatRow, type ChatsView } from "@/components/sessions/ChatHistory";
import { useSessions } from "@/components/sessions/SessionsContext";
import {
  listChatTopics,
  listSharedSessions,
  searchSessions,
  type ChatTopic,
  type SessionSummary,
} from "@/lib/api";
import {
  CHANNEL_LABELS,
  CHANNEL_ORDER,
  countByChannel,
  filterSessions,
  type SessionChannel,
} from "@/lib/sessionChannel";

type ChannelFilter = SessionChannel | "all";

const VIEW_KEY = "oe.chats.view";
// While older chats are being filed, how often and how long to look again.
const ORGANIZE_POLL_MS = 3000;
const ORGANIZE_POLLS = 20;

// Date or Topic, remembered per browser; the server render is always Date.
const viewListeners = new Set<() => void>();
let sessionView: ChatsView | null = null;

function storedView(): ChatsView {
  if (sessionView) return sessionView;
  try {
    return window.localStorage.getItem(VIEW_KEY) === "topic" ? "topic" : "date";
  } catch {
    return "date";
  }
}

function saveView(next: ChatsView): void {
  sessionView = next;
  try {
    window.localStorage.setItem(VIEW_KEY, next);
  } catch {
    // Private mode: the choice lasts this visit.
  }
  viewListeners.forEach((fn) => fn());
}

function subscribeView(fn: () => void): () => void {
  viewListeners.add(fn);
  return () => viewListeners.delete(fn);
}

// Full conversation history: every chat, including conversations that
// arrived through Slack, Telegram and Discord. Each row opens its chat;
// Delete is in the row's ⋯ menu and asks first. Chats others shared with
// the viewer list apart, under "Shared with you", and open to read.
export default function ChatsPage() {
  const router = useRouter();
  const { sessions, loaded, error, refresh, remove } = useSessions();
  const [channel, setChannel] = useState<ChannelFilter>("all");
  const [query, setQuery] = useState("");
  // What the server found for `query` in titles and messages; null until it
  // answers (the list filters titles locally meanwhile) or with no query.
  const [found, setFound] = useState<{ query: string; sessions: SessionSummary[] } | null>(null);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const view = useSyncExternalStore(subscribeView, storedView, () => "date" as const);
  const chooseView = saveView;

  // The caller's topics, loaded for the Topic view. While older chats are
  // being filed it looks again every few seconds, reloading the list too so
  // each chat lands under its topic.
  const [topics, setTopics] = useState<ChatTopic[]>([]);
  const [organizing, setOrganizing] = useState(false);
  const [topicsVersion, setTopicsVersion] = useState(0);
  useEffect(() => {
    if (view !== "topic") return;
    let live = true;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      listChatTopics()
        .then((res) => {
          if (!live) return;
          setTopics(res.topics);
          setOrganizing(res.organizing);
          if (polls > 0) refresh();
          if (res.organizing && polls < ORGANIZE_POLLS) {
            polls += 1;
            timer = setTimeout(load, ORGANIZE_POLL_MS);
          }
        })
        .catch(() => {
          if (live) setOrganizing(false);
        });
    };
    load();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [view, topicsVersion, refresh]);
  const changed = () => {
    refresh();
    setTopicsVersion((v) => v + 1);
  };

  // Chats teammates shared with the viewer, to write in with them. Kept out
  // of the channel tabs and counts. A failure just leaves the section hidden.
  const [shared, setShared] = useState<SessionSummary[]>([]);
  const [sharedVersion, setSharedVersion] = useState(0);
  useEffect(() => {
    let live = true;
    listSharedSessions()
      .then((list) => {
        if (live) setShared(list);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [sharedVersion]);

  const trimmed = query.trim();
  useEffect(() => {
    if (!trimmed) return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      searchSessions(trimmed, ctrl.signal)
        .then((list) => setFound({ query: trimmed, sessions: list }))
        .catch(() => {
          // Aborted by the next keystroke, or the search failed: the local
          // title filter stays on screen.
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [trimmed]);
  const results = found && found.query === trimmed ? found.sessions : null;
  // A chat deleted from the results leaves them too.
  const shown = useMemo(() => {
    if (!results) return null;
    const live = new Set(sessions.map((s) => s.session_id));
    return results.filter((s) => live.has(s.session_id));
  }, [results, sessions]);

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
  // Chats under a topic (or area) whose name has the words, alongside the
  // ones whose title or messages do.
  const topicHits = useMemo(() => {
    const q = trimmed.toLowerCase();
    if (!q || topics.length === 0) return [];
    const ids = new Set(
      topics
        .filter((t) => t.name.toLowerCase().includes(q) || (t.area ?? "").toLowerCase().includes(q))
        .map((t) => t.id),
    );
    return sessions.filter((s) => s.topic_id != null && ids.has(s.topic_id));
  }, [trimmed, topics, sessions]);
  const visible = useMemo(() => {
    const base = shown
      ? filterSessions(shown, { channel: activeChannel, query: "" })
      : filterSessions(sessions, { channel: activeChannel, query });
    if (topicHits.length === 0) return base;
    const have = new Set(base.map((s) => s.session_id));
    const extra = filterSessions(topicHits, { channel: activeChannel, query: "" }).filter((s) => !have.has(s.session_id));
    if (extra.length === 0) return base;
    const order = new Map(sessions.map((s, i) => [s.session_id, i]));
    return [...base, ...extra].sort(
      (a, b) => (order.get(a.session_id) ?? 0) - (order.get(b.session_id) ?? 0),
    );
  }, [shown, sessions, activeChannel, query, topicHits]);
  const searching = trimmed.length > 0;
  const shownCounts = useMemo(() => (shown ? countByChannel(shown) : counts), [shown, counts]);

  // Hidden while searching: the search covers the viewer's own chats.
  const sharedSection =
    shared.length > 0 && !searching ? (
      <section aria-labelledby="shared-with-you" className="mt-8 first:mt-0">
        <h2 id="shared-with-you" className="px-1 mb-2 text-sm font-semibold text-fg-muted">
          Shared with you <span className="font-normal text-fg-subtle">{shared.length}</span>
        </h2>
        <div className="space-y-2">
          {shared.map((s) => (
            <ChatRow
              key={s.session_id}
              session={s}
              onSelect={(id) => router.push(`/chats/shared/${encodeURIComponent(id)}`)}
              onChanged={() => setSharedVersion((v) => v + 1)}
            />
          ))}
        </div>
      </section>
    ) : null;

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

          <div className="relative mb-3">
            <Icon
              name="search"
              size="w-4 h-4"
              className="absolute left-3.5 top-1/2 -translate-y-1/2 text-fg-subtle pointer-events-none"
            />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={view === "topic" ? "Search chats and topics" : "Search titles and messages"}
              aria-label="Search conversations"
              className="w-full h-11 rounded-xl bg-surface-elevated border border-line pl-10 pr-3 text-[15px] text-fg placeholder:text-fg-subtle focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20"
            />
          </div>
          <div className="flex items-center gap-3 mb-5">
            <div
              role="tablist"
              aria-label="Filter by channel"
              className="flex-1 min-w-0 flex items-center gap-2 overflow-x-auto [scrollbar-width:none]"
            >
              {tabs.map((t) => {
                const active = activeChannel === t;
                const count =
                  t === "all" ? (shown ?? sessions).length : (shownCounts[t] ?? 0);
                return (
                  <button
                    key={t}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => setChannel(t)}
                    className={`flex-shrink-0 whitespace-nowrap min-h-[36px] px-3.5 text-sm font-medium rounded-full border transition cursor-pointer ${
                      active
                        ? "border-transparent bg-accent/10 text-accent"
                        : "border-line text-fg-muted hover:text-fg hover:bg-surface-overlay"
                    }`}
                  >
                    {t === "all" ? "All" : CHANNEL_LABELS[t]}
                    <span className="ml-1.5 text-fg-subtle">{count}</span>
                  </button>
                );
              })}
            </div>
            <div
              role="tablist"
              aria-label="Group chats by"
              className="flex-shrink-0 inline-flex items-center gap-1 p-1 rounded-full border border-line"
            >
              {(["date", "topic"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  role="tab"
                  aria-selected={view === v}
                  onClick={() => chooseView(v)}
                  className={`min-h-[30px] px-3.5 text-sm font-medium rounded-full transition cursor-pointer ${
                    view === v ? "bg-accent/10 text-accent" : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
                  }`}
                >
                  {v === "date" ? "Date" : "Topic"}
                </button>
              ))}
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
            <>
              {sharedSection}
              <p className="px-1 py-6 text-[15px] text-fg-muted">
                No conversations yet. Start one with New chat.
              </p>
            </>
          ) : (
            <>
            {view === "topic" && organizing && (
              <p role="status" className="px-1 pb-3 text-sm text-fg-muted">
                Grouping your earlier chats by topic…
              </p>
            )}
            <ChatHistory
              sessions={visible}
              searching={searching}
              query={shown ? trimmed : ""}
              view={view}
              topics={topics}
              onSelect={(id) => {
                // Opened from a message match: the chat steps through the matches.
                const hit = shown?.find((s) => s.session_id === id);
                const find = hit && hit.match_count ? `&find=${encodeURIComponent(trimmed)}` : "";
                router.push(`/?session=${encodeURIComponent(id)}${find}`);
              }}
              onDelete={remove}
              onChanged={changed}
            />
            {sharedSection}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
