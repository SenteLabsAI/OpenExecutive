"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import Icon from "@/components/Icon";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import {
  getPeopleViewer,
  getSessionShares,
  leaveSharedChat,
  listPeople,
  mergeChatTopic,
  renameTopicArea,
  setSessionShares,
  ungroupChatTopic,
  ungroupTopicArea,
  updateChatTopic,
  updateSession,
  type ChatTopic,
  type Person,
  type SessionSummary,
} from "@/lib/api";
import { downloadChat } from "@/lib/downloadChat";
import { formatRelativeTime } from "@/lib/relativeTime";
import { highlightParts, matchLabel } from "@/lib/chatSearch";
import { sessionChannel, sessionTitle } from "@/lib/sessionChannel";
import { ChannelLogo } from "@/components/sessions/ChannelLogo";
import {
  activeLabel,
  groupSessionsByDate,
  layoutTopics,
  type GroupKey,
  type TopicEntry,
  type TopicGroup,
} from "@/lib/sessionGroups";

const GROUP_CAP = 20;
// Chats an open topic shows before "Show N more".
const TOPIC_CAP = 3;
// How many topics a band shows before "Show N more topics".
const BAND_CAP = 5;
const DEFAULT_COLLAPSED = new Set<string>(["older" satisfies GroupKey, "other"]);

export type ChatsView = "date" | "topic";

interface ChatHistoryProps {
  /** Already filtered by channel and search; newest first. */
  sessions: SessionSummary[];
  /** While a search is active every group is forced open so no match is hidden. */
  searching: boolean;
  /** The words the server searched for, to mark in each result's snippet. */
  query?: string;
  /** Group by date (the default) or by topic. */
  view?: ChatsView;
  /** The caller's topics, for the Topic view and a row's Move to topic. */
  topics?: ChatTopic[];
  onSelect: (sessionId: string) => void;
  /** Deletes after the row has asked; resolves false when it failed. */
  onDelete: (sessionId: string) => Promise<boolean>;
  /** Reloads the list after a rename, a pin or a move. */
  onChanged: () => void;
}

interface ListGroup {
  key: string;
  label: string;
  topic?: ChatTopic;
  items: SessionSummary[];
}

// Conversation list for /chats, grouped by date or by topic — the full
// history the sidebar's short Recent list links to.
export default function ChatHistory({
  sessions,
  searching,
  query = "",
  view = "date",
  topics = [],
  onSelect,
  onDelete,
  onChanged,
}: ChatHistoryProps) {
  const [collapsedOverride, setCollapsedOverride] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Set<string>>(new Set());

  const groups: ListGroup[] = useMemo(() => (view === "topic" ? [] : groupSessionsByDate(sessions)), [view, sessions]);

  if (view === "topic") {
    return (
      <TopicHistory
        sessions={sessions}
        searching={searching}
        query={query}
        topics={topics}
        onSelect={onSelect}
        onDelete={onDelete}
        onChanged={onChanged}
      />
    );
  }

  if (groups.length === 0) {
    return <p className="px-1 py-6 text-[15px] text-fg-muted">No conversations match.</p>;
  }

  // Stored collapse state (default-collapsed unless the user toggled it). While
  // searching we force every group open so matches are never hidden, but the
  // stored state is preserved and re-applies once the query is cleared.
  const storedCollapsed = (key: string) =>
    key in collapsedOverride ? collapsedOverride[key] : DEFAULT_COLLAPSED.has(key);
  const isCollapsed = (key: string) => (searching ? false : storedCollapsed(key));
  const toggleCollapse = (key: string) =>
    setCollapsedOverride((prev) => ({ ...prev, [key]: !storedCollapsed(key) }));
  const revealAll = (key: string) => setShowAll((prev) => new Set(prev).add(key));

  return (
    <div className="space-y-4">
      {groups.map((group) => {
        const collapsed = isCollapsed(group.key);
        const expandedAll = searching || showAll.has(group.key);
        const visible = expandedAll ? group.items : group.items.slice(0, GROUP_CAP);
        const hiddenCount = group.items.length - visible.length;
        return (
          <section key={group.key}>
            <div className="flex flex-wrap items-center gap-1">
              <button
                type="button"
                onClick={() => toggleCollapse(group.key)}
                aria-expanded={!collapsed}
                className="flex-1 min-w-0 flex items-center gap-2 min-h-[40px] px-1 text-fg-muted hover:text-fg transition-colors cursor-pointer"
              >
                <Icon
                  name="chevron-right"
                  size="w-3.5 h-3.5"
                  className={`flex-shrink-0 transition-transform ${collapsed ? "" : "rotate-90"}`}
                />
                <span className="text-sm font-semibold truncate">{group.label}</span>
                <span className="text-sm text-fg-subtle">{group.items.length}</span>
              </button>
              {group.topic && <TopicMenu topic={group.topic} topics={topics} onChanged={onChanged} />}
            </div>
            {!collapsed && (
              <div className="mt-1 space-y-2">
                {visible.map((s) => (
                  <ChatRow
                    key={s.session_id}
                    session={s}
                    query={query}
                    topics={topics}
                    onSelect={onSelect}
                    onDelete={onDelete}
                    onChanged={onChanged}
                  />
                ))}
                {hiddenCount > 0 && (
                  <button
                    type="button"
                    onClick={() => revealAll(group.key)}
                    className="w-full text-left min-h-[40px] px-4 text-sm font-medium text-fg-muted hover:text-fg cursor-pointer"
                  >
                    Show {hiddenCount} more
                  </button>
                )}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

// The Topic view: pinned chats, then topics (and areas over related ones)
// in bands by how long since their last chat, then Other. The active band
// shows its topics; a quiet band folds into one line until opened. A band
// shows its five most recent topics, one short line each, then "Show N more
// topics"; an open topic shows its newest few chats. A search opens everything.
function TopicHistory({
  sessions,
  searching,
  query,
  topics,
  onSelect,
  onDelete,
  onChanged,
}: {
  sessions: SessionSummary[];
  searching: boolean;
  query: string;
  topics: ChatTopic[];
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => Promise<boolean>;
  onChanged: () => void;
}) {
  const layout = useMemo(() => layoutTopics(sessions, topics), [sessions, topics]);
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Set<string>>(new Set());

  const firstActive = layout.bands[0]?.key === "active" ? layout.bands[0].entries[0] : undefined;
  // Open by default: the most recent active topic (and an area's newest).
  const openByDefault = new Set<string>(
    firstActive ? [firstActive.key, ...(firstActive.children[0] ? [firstActive.children[0].key] : [])] : [],
  );
  const isOpen = (key: string) => searching || (key in opened ? opened[key] : openByDefault.has(key));
  const toggle = (key: string) => setOpened((prev) => ({ ...prev, [key]: !isOpen(key) }));

  if (!layout.pinned && !layout.other && layout.bands.length === 0) {
    return <p className="px-1 py-6 text-[15px] text-fg-muted">No conversations match.</p>;
  }

  const rows = (key: string, items: SessionSummary[], indent: string) => {
    const all = searching || showAll.has(key);
    const visible = all ? items : items.slice(0, TOPIC_CAP);
    const hidden = items.length - visible.length;
    return (
      <div className={`mt-1 mb-2 space-y-2 ${indent}`}>
        {visible.map((s) => (
          <ChatRow
            key={s.session_id}
            session={s}
            query={query}
            topics={topics}
            onSelect={onSelect}
            onDelete={onDelete}
            onChanged={onChanged}
          />
        ))}
        {hidden > 0 && (
          <button
            type="button"
            onClick={() => setShowAll((prev) => new Set(prev).add(key))}
            className="w-full text-left min-h-[40px] px-4 text-sm font-medium text-accent hover:text-fg cursor-pointer"
          >
            Show {hidden} more
          </button>
        )}
      </div>
    );
  };

  const header = ({
    key,
    label,
    count,
    preview,
    when,
    menu,
    strong,
    indent = "",
  }: {
    key: string;
    label: string;
    count: number;
    preview?: string;
    when?: string;
    menu?: ReactNode;
    strong?: boolean;
    indent?: string;
  }) => {
    const open = isOpen(key);
    return (
      <div className={`flex flex-wrap items-center gap-1 border-b border-line ${indent}`}>
        <button
          type="button"
          onClick={() => toggle(key)}
          aria-expanded={open}
          className="flex-1 min-w-0 flex items-center gap-2 min-h-[44px] px-1 text-left text-fg-muted hover:text-fg transition-colors cursor-pointer"
        >
          <Icon
            name="chevron-right"
            size="w-3.5 h-3.5"
            className={`flex-shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
          />
          <span className={`truncate font-semibold ${strong ? "text-[15px] text-fg" : "text-sm text-fg"}`}>{label}</span>
          <span className="text-sm text-fg-subtle flex-shrink-0">{count}</span>
          {!open && preview && (
            <span className="hidden sm:inline min-w-0 truncate text-sm text-fg-subtle">{preview}</span>
          )}
          {when && <span className="ml-auto pl-2 flex-shrink-0 text-xs text-fg-subtle">{when}</span>}
        </button>
        {menu}
      </div>
    );
  };

  const topicBlock = (g: TopicGroup, indent: string) => (
    <section key={g.key}>
      {header({
        key: g.key,
        label: g.label,
        count: g.items.length,
        when: activeLabel(g.items[0]?.updated_at ?? ""),
        menu: g.topic ? <TopicMenu topic={g.topic} topics={topics} onChanged={onChanged} /> : undefined,
        indent,
      })}
      {isOpen(g.key) && rows(g.key, g.items, indent)}
    </section>
  );

  const entryBlock = (e: TopicEntry) =>
    e.area ? (
      <section key={e.key}>
        {header({
          key: e.key,
          label: e.label,
          count: e.items.length,
          preview: e.children.map((g) => g.label).join(" · "),
          when: activeLabel(e.lastActive),
          menu: <AreaMenu area={e.area} onChanged={onChanged} />,
          strong: true,
        })}
        {isOpen(e.key) && e.children.map((g) => topicBlock(g, "ml-6"))}
      </section>
    ) : (
      topicBlock({ key: e.key, label: e.label, topic: e.topic, items: e.items }, "")
    );

  const entries = (key: string, list: TopicEntry[]) => {
    const all = searching || showAll.has(key);
    const visible = all ? list : list.slice(0, BAND_CAP);
    const hidden = list.length - visible.length;
    return (
      <>
        {visible.map(entryBlock)}
        {hidden > 0 && (
          <button
            type="button"
            onClick={() => setShowAll((prev) => new Set(prev).add(key))}
            className="w-full text-left min-h-[40px] px-6 text-sm font-medium text-accent hover:text-fg border-b border-line cursor-pointer"
          >
            Show {hidden} more topic{hidden === 1 ? "" : "s"}
          </button>
        )}
      </>
    );
  };

  return (
    <div>
      {layout.pinned && (
        <section>
          {header({ key: "pinned", label: "Pinned", count: layout.pinned.items.length })}
          {isOpen("pinned") && rows("pinned", layout.pinned.items, "")}
        </section>
      )}
      {layout.bands.map((band) => {
        const bandKey = `band:${band.key}`;
        return (
          <section key={band.key} aria-label={band.label} className="mt-6">
            <h2 className="px-1 mb-1 text-xs font-semibold uppercase tracking-wider text-fg-subtle">{band.label}</h2>
            {band.key === "active" ? (
              entries(bandKey, band.entries)
            ) : (
              <>
                {header({
                  key: bandKey,
                  label: `${band.entries.length} topic${band.entries.length === 1 ? "" : "s"}`,
                  count: band.entries.reduce((n, e) => n + e.items.length, 0),
                  preview: band.entries.map((e) => e.label).join(" · "),
                })}
                {isOpen(bandKey) && <div className="ml-6">{entries(bandKey, band.entries)}</div>}
              </>
            )}
          </section>
        );
      })}
      {layout.other && (
        <section className="mt-6">
          {header({ key: "other", label: "Other", count: layout.other.items.length, preview: "Chats under no topic" })}
          {isOpen("other") && rows("other", layout.other.items, "")}
        </section>
      )}
    </div>
  );
}

// An area heading's ⋯ menu: Rename area and Ungroup area. Either is kept:
// the model doesn't put an ungrouped area's topics back under one.
function AreaMenu({ area, onChanged }: { area: string; onChanged: () => void }) {
  const [panel, setPanel] = useState<"rename" | "ungroup" | null>(null);
  const [name, setName] = useState(area);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setPanel(null);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't update this area.");
    } finally {
      setBusy(false);
    }
  };

  const open = (next: "rename" | "ungroup") => {
    setError(null);
    setName(area);
    setPanel(next);
  };

  return (
    <>
      <OverflowMenu
        label={`More actions for ${area}`}
        items={[
          { label: "Rename area", onSelect: () => open("rename") },
          { label: "Ungroup area", onSelect: () => open("ungroup") },
        ]}
      />
      {panel === "rename" && (
        <form
          className="basis-full mt-1 mb-2 flex flex-wrap items-center gap-2 rounded-2xl border border-line bg-surface-elevated px-4 py-3"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = name.trim();
            if (trimmed) void run(() => renameTopicArea(area, trimmed.slice(0, 40)));
          }}
        >
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setPanel(null);
              }
            }}
            maxLength={40}
            autoFocus
            aria-label="Area name"
            className="flex-1 min-w-[12rem] h-10 rounded-lg bg-surface border border-line px-3 text-[15px] text-fg focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20"
          />
          <Button size="sm" variant="primary" type="submit" disabled={!name.trim() || busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => setPanel(null)} disabled={busy}>
            Cancel
          </Button>
        </form>
      )}
      {panel === "ungroup" && (
        <div
          role="alert"
          className="basis-full mt-1 mb-2 flex flex-wrap items-center gap-2 rounded-2xl border border-line bg-surface-elevated px-4 py-3"
        >
          <p className="mr-auto text-sm text-fg">Ungroup {area}? Its topics stay, each on its own.</p>
          <Button size="sm" variant="primary" onClick={() => void run(() => ungroupTopicArea(area))} disabled={busy}>
            {busy ? "Ungrouping…" : "Ungroup"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setPanel(null)} disabled={busy}>
            Cancel
          </Button>
        </div>
      )}
      {error && !panel && (
        <p role="alert" className="basis-full mt-1 rounded-2xl border border-line bg-surface-elevated px-4 py-3 text-sm text-fg">
          {error}
        </p>
      )}
    </>
  );
}

type TopicPanel = "rename" | "merge" | "ungroup" | null;

// A topic header's ⋯ menu: Rename, Merge into…, Pin and Ungroup. Each
// panel opens under the header; what the owner does here is kept.
function TopicMenu({
  topic,
  topics,
  onChanged,
}: {
  topic: ChatTopic;
  topics: ChatTopic[];
  onChanged: () => void;
}) {
  const [panel, setPanel] = useState<TopicPanel>(null);
  const [name, setName] = useState(topic.name);
  const [into, setInto] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const others = topics.filter((t) => t.id !== topic.id);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setPanel(null);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't update this topic.");
    } finally {
      setBusy(false);
    }
  };

  const open = (next: TopicPanel) => {
    setError(null);
    setName(topic.name);
    setInto("");
    setPanel(next);
  };

  const items = [
    { label: "Rename topic", onSelect: () => open("rename") },
    ...(others.length ? [{ label: "Merge into…", onSelect: () => open("merge") }] : []),
    {
      label: topic.pinned_at ? "Unpin topic" : "Pin topic",
      onSelect: () => void run(() => updateChatTopic(topic.id, { pinned: !topic.pinned_at })),
    },
    { label: "Ungroup", onSelect: () => open("ungroup") },
  ];

  return (
    <>
      <OverflowMenu label={`More actions for ${topic.name}`} items={items} />
      {panel === "rename" && (
        <form
          className="basis-full mt-1 flex flex-wrap items-center gap-2 rounded-2xl border border-line bg-surface-elevated px-4 py-3"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = name.trim();
            if (trimmed) void run(() => updateChatTopic(topic.id, { name: trimmed.slice(0, 40) }));
          }}
        >
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setPanel(null);
              }
            }}
            maxLength={40}
            autoFocus
            aria-label="Topic name"
            className="flex-1 min-w-[12rem] h-10 rounded-lg bg-surface border border-line px-3 text-[15px] text-fg focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20"
          />
          <Button size="sm" variant="primary" type="submit" disabled={!name.trim() || busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => setPanel(null)} disabled={busy}>
            Cancel
          </Button>
        </form>
      )}
      {panel === "merge" && (
        <div className="basis-full mt-1 flex flex-wrap items-center gap-2 rounded-2xl border border-line bg-surface-elevated px-4 py-3">
          <label className="text-sm text-fg" htmlFor={`merge-${topic.id}`}>
            Move its chats to
          </label>
          <select
            id={`merge-${topic.id}`}
            value={into}
            onChange={(e) => setInto(e.target.value ? Number(e.target.value) : "")}
            className="flex-1 min-w-[10rem] h-10 rounded-lg bg-surface border border-line px-3 text-[15px] text-fg"
          >
            <option value="">Choose a topic</option>
            {others.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="primary"
            disabled={into === "" || busy}
            onClick={() => into !== "" && void run(() => mergeChatTopic(topic.id, into))}
          >
            {busy ? "Merging…" : "Merge"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setPanel(null)} disabled={busy}>
            Cancel
          </Button>
        </div>
      )}
      {panel === "ungroup" && (
        <div
          role="alert"
          className="basis-full mt-1 flex flex-wrap items-center gap-2 rounded-2xl border border-line bg-surface-elevated px-4 py-3"
        >
          <p className="mr-auto text-sm text-fg">Ungroup {topic.name}? Its chats stay, under Other.</p>
          <Button size="sm" variant="primary" onClick={() => void run(() => ungroupChatTopic(topic.id))} disabled={busy}>
            {busy ? "Ungrouping…" : "Ungroup"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setPanel(null)} disabled={busy}>
            Cancel
          </Button>
        </div>
      )}
      {error && !panel && (
        <p role="alert" className="basis-full mt-1 rounded-2xl border border-line bg-surface-elevated px-4 py-3 text-sm text-fg">
          {error}
        </p>
      )}
    </>
  );
}

type RowPanel = "delete" | "rename" | "share" | "leave" | "move" | null;

export function ChatRow({
  session,
  query = "",
  topics,
  onSelect,
  onDelete,
  onChanged,
}: {
  session: SessionSummary;
  query?: string;
  /** The owner's topics; with them the menu offers Move to topic. */
  topics?: ChatTopic[];
  onSelect: (sessionId: string) => void;
  /** Absent for a chat shared with the viewer, which only its owner deletes. */
  onDelete?: (sessionId: string) => Promise<boolean>;
  onChanged?: () => void;
}) {
  // Delete, Rename and Share open a panel here, in the row; the ⋯ menu
  // holds them all.
  const [panel, setPanel] = useState<RowPanel>(null);
  const [deleting, setDeleting] = useState(false);
  const [failed, setFailed] = useState(false);
  // What the last quick action (pin, download) couldn't do, if anything.
  const [actionError, setActionError] = useState<string | null>(null);
  const channel = sessionChannel(session.session_id);
  const sharedWithYou = !!session.shared_with_you;
  const messages =
    matchLabel(session.match_count) ??
    `${session.message_count} message${session.message_count === 1 ? "" : "s"}`;

  const confirmDelete = async () => {
    if (!onDelete) return;
    setDeleting(true);
    setFailed(false);
    const ok = await onDelete(session.session_id);
    // On success the row leaves the list; on failure it stays and says so.
    if (!ok) {
      setDeleting(false);
      setFailed(true);
    }
  };

  const togglePin = async () => {
    setActionError(null);
    try {
      await updateSession(session.session_id, { pinned: !session.pinned_at });
      onChanged?.();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't pin this chat.");
    }
  };

  const download = async () => {
    setActionError(null);
    try {
      await downloadChat(session.session_id);
    } catch {
      setActionError("Couldn't download this chat.");
    }
  };

  const open = (next: RowPanel) => {
    setActionError(null);
    setFailed(false);
    setPanel(next);
  };

  const leave = async () => {
    setDeleting(true);
    setActionError(null);
    try {
      await leaveSharedChat(session.session_id);
      onChanged?.();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't leave this chat.");
    } finally {
      setDeleting(false);
      setPanel(null);
    }
  };

  const menu = sharedWithYou
    ? [
        { label: "Download", onSelect: () => void download() },
        { label: "Leave chat", danger: true, onSelect: () => open("leave") },
      ]
    : [
        { label: session.pinned_at ? "Unpin" : "Pin", onSelect: () => void togglePin() },
        { label: "Rename", onSelect: () => open("rename") },
        ...(topics ? [{ label: "Move to topic", onSelect: () => open("move") }] : []),
        { label: "Share", onSelect: () => open("share") },
        { label: "Download", onSelect: () => void download() },
        ...(onDelete ? [{ label: "Delete chat", danger: true, onSelect: () => open("delete") }] : []),
      ];

  return (
    <div className="rounded-2xl border border-line bg-surface-elevated transition-colors hover:border-line-strong">
      <div className="flex items-center gap-1 pr-2">
        <button
          type="button"
          onClick={() => onSelect(session.session_id)}
          className="flex-1 min-w-0 text-left px-4 py-3.5 cursor-pointer rounded-2xl focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        >
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-base font-medium text-fg truncate">{sessionTitle(session)}</span>
            <ChannelLogo channel={channel} label />
          </span>
          <span className="block text-sm text-fg-subtle mt-0.5">
            {sharedWithYou ? (
              <>
                {session.shared_by ?? "Someone on your team"} added you ·{" "}
                {formatRelativeTime(session.shared_at ?? session.updated_at)}
              </>
            ) : (
              <>
                {session.pinned_at && <span className="text-fg-muted">Pinned · </span>}
                {formatRelativeTime(session.updated_at)} · {messages}
              </>
            )}
          </span>
          {session.snippet && (
            <span className="block text-sm text-fg-muted mt-1.5 line-clamp-2 break-words">
              {highlightParts(session.snippet, query).map((p, i) =>
                p.match ? (
                  <mark key={i} className="bg-accent/25 text-fg rounded px-0.5">
                    {p.text}
                  </mark>
                ) : (
                  <span key={i}>{p.text}</span>
                ),
              )}
            </span>
          )}
        </button>
        <OverflowMenu label="More actions for this chat" items={menu} />
      </div>
      {actionError && (
        <p role="alert" className="border-t border-line px-4 py-3 text-sm text-fg">
          {actionError}
        </p>
      )}
      {(panel === "delete" || failed) && (
        <div role="alert" className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          <p className="mr-auto text-sm text-fg">
            {failed ? "Couldn't delete this chat. Try again?" : "Delete this chat? This cannot be undone."}
          </p>
          <Button size="sm" variant="danger" onClick={() => void confirmDelete()} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setPanel(null);
              setFailed(false);
            }}
            disabled={deleting}
          >
            Cancel
          </Button>
        </div>
      )}
      {panel === "rename" && (
        <RenamePanel
          session={session}
          onDone={(changed) => {
            setPanel(null);
            if (changed) onChanged?.();
          }}
        />
      )}
      {panel === "leave" && (
        <div role="alert" className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          <p className="mr-auto text-sm text-fg">
            Leave this chat? {session.shared_by ?? "Its owner"} can add you again.
          </p>
          <Button size="sm" variant="danger" onClick={() => void leave()} disabled={deleting}>
            {deleting ? "Leaving…" : "Leave"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setPanel(null)} disabled={deleting}>
            Cancel
          </Button>
        </div>
      )}
      {panel === "move" && topics && (
        <MovePanel
          session={session}
          topics={topics}
          onDone={(changed) => {
            setPanel(null);
            if (changed) onChanged?.();
          }}
        />
      )}
      {panel === "share" && <SharePanel sessionId={session.session_id} onClose={() => setPanel(null)} />}
    </div>
  );
}

function RenamePanel({ session, onDone }: { session: SessionSummary; onDone: (changed: boolean) => void }) {
  const [title, setTitle] = useState(sessionTitle(session));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trimmed = title.trim();

  const save = async () => {
    if (!trimmed || saving) return;
    setSaving(true);
    setError(null);
    try {
      await updateSession(session.session_id, { title: trimmed.slice(0, 120) });
      onDone(true);
    } catch (err) {
      setSaving(false);
      setError(err instanceof Error ? err.message : "Couldn't rename this chat.");
    }
  };

  return (
    <form
      className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            onDone(false);
          }
        }}
        maxLength={120}
        autoFocus
        aria-label="Chat name"
        className="flex-1 min-w-[12rem] h-10 rounded-lg bg-surface border border-line px-3 text-[15px] text-fg focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20"
      />
      <Button size="sm" variant="primary" type="submit" disabled={!trimmed || saving}>
        {saving ? "Saving…" : "Save"}
      </Button>
      <Button size="sm" variant="ghost" type="button" onClick={() => onDone(false)} disabled={saving}>
        Cancel
      </Button>
      {error && (
        <p role="alert" className="basis-full text-sm text-red-400">
          {error}
        </p>
      )}
    </form>
  );
}

const NEW_TOPIC = "new";
const NO_TOPIC = "none";

// File a chat under one of the owner's topics, a new one, or none. The
// choice sticks: the chat is never filed again on its own.
function MovePanel({
  session,
  topics,
  onDone,
}: {
  session: SessionSummary;
  topics: ChatTopic[];
  onDone: (changed: boolean) => void;
}) {
  const [choice, setChoice] = useState<string>(
    session.topic_id != null ? String(session.topic_id) : topics.length ? String(topics[0].id) : NEW_TOPIC,
  );
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = choice !== NEW_TOPIC || name.trim().length > 0;

  const save = async () => {
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    try {
      await updateSession(
        session.session_id,
        choice === NEW_TOPIC
          ? { topic_name: name.trim().slice(0, 40) }
          : { topic_id: choice === NO_TOPIC ? null : Number(choice) },
      );
      onDone(true);
    } catch (err) {
      setSaving(false);
      setError(err instanceof Error ? err.message : "Couldn't move this chat.");
    }
  };

  return (
    <form
      className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <select
        value={choice}
        onChange={(e) => setChoice(e.target.value)}
        aria-label="Topic"
        className="flex-1 min-w-[10rem] h-10 rounded-lg bg-surface border border-line px-3 text-[15px] text-fg"
      >
        {topics.map((t) => (
          <option key={t.id} value={String(t.id)}>
            {t.name}
          </option>
        ))}
        <option value={NEW_TOPIC}>New topic…</option>
        <option value={NO_TOPIC}>No topic</option>
      </select>
      {choice === NEW_TOPIC && (
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={40}
          autoFocus
          placeholder="Topic name"
          aria-label="New topic name"
          className="flex-1 min-w-[10rem] h-10 rounded-lg bg-surface border border-line px-3 text-[15px] text-fg focus:outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20"
        />
      )}
      <Button size="sm" variant="primary" type="submit" disabled={!ready || saving}>
        {saving ? "Moving…" : "Move"}
      </Button>
      <Button size="sm" variant="ghost" type="button" onClick={() => onDone(false)} disabled={saving}>
        Cancel
      </Button>
      {error && (
        <p role="alert" className="basis-full text-sm text-red-400">
          {error}
        </p>
      )}
    </form>
  );
}

interface ShareState {
  people: Person[];
  chosen: Set<number>;
}

// Who the chat is shared with: team members only, never the owner. They join
// it and write in it with the owner. Loaded when the panel opens (it mounts
// then), keyed by the chat it is for. `onSaved` hears every change saved.
export function SharePanel({
  sessionId,
  onClose,
  onSaved,
}: {
  sessionId: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [loaded, setLoaded] = useState<ShareState | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // State is only set once the requests answer, never synchronously here.
  const fetchShares = useCallback(
    (isLive: () => boolean) => {
      Promise.all([listPeople(), getPeopleViewer(), getSessionShares(sessionId)])
        .then(([people, viewer, shares]) => {
          if (!isLive()) return;
          const team = people.filter(
            (p) => p.kind === "team" && !p.archived && p.id !== viewer.person_id,
          );
          setLoaded({ people: team, chosen: new Set(shares.map((s) => s.person_id)) });
        })
        .catch(() => {
          if (isLive()) setLoadError(true);
        });
    },
    [sessionId],
  );
  useEffect(() => {
    let live = true;
    fetchShares(() => live);
    return () => {
      live = false;
    };
  }, [fetchShares]);
  const load = () => {
    setLoadError(false);
    fetchShares(() => true);
  };

  const toggle = (id: number) => {
    setSaved(false);
    setLoaded((prev) => {
      if (!prev) return prev;
      const chosen = new Set(prev.chosen);
      if (chosen.has(id)) chosen.delete(id);
      else chosen.add(id);
      return { ...prev, chosen };
    });
  };

  const save = async () => {
    if (!loaded) return;
    setSaving(true);
    setError(null);
    try {
      const shares = await setSessionShares(sessionId, Array.from(loaded.chosen));
      setLoaded({ ...loaded, chosen: new Set(shares.map((s) => s.person_id)) });
      setSaved(true);
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't share this chat.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border-t border-line px-4 py-3 space-y-3">
      <div>
        <p className="text-sm text-fg">Share this chat with:</p>
        <p className="text-sm text-fg-muted">They join the chat and can write in it with you.</p>
      </div>
      {loadError ? (
        <p role="alert" className="text-sm text-fg-muted">
          Couldn&apos;t load your team.{" "}
          <button type="button" onClick={load} className="font-medium text-accent hover:underline cursor-pointer">
            Try again
          </button>
        </p>
      ) : !loaded ? (
        <p className="text-sm text-fg-muted">Loading your team…</p>
      ) : loaded.people.length === 0 ? (
        <p className="text-sm text-fg-muted">There&apos;s no one else on your team to share with yet.</p>
      ) : (
        <ul className="max-h-60 overflow-y-auto space-y-0.5">
          {loaded.people.map((p) => (
            <li key={p.id}>
              <label className="flex items-center gap-3 min-h-[2.5rem] px-2 rounded-lg hover:bg-surface-overlay cursor-pointer">
                <input
                  type="checkbox"
                  checked={loaded.chosen.has(p.id)}
                  onChange={() => toggle(p.id)}
                  className="h-4 w-4 flex-shrink-0 accent-[rgb(var(--accent-strong))]"
                />
                <span className="text-[15px] text-fg">{p.full_name}</span>
                {p.role && <span className="text-sm text-fg-subtle truncate">{p.role}</span>}
              </label>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-400">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {saved && (
          <p role="status" className="mr-auto text-sm text-fg-muted">
            {loaded && loaded.chosen.size > 0 ? "Shared." : "Not shared with anyone."}
          </p>
        )}
        <Button
          size="sm"
          variant="primary"
          className={saved ? "" : "ml-auto"}
          onClick={() => void save()}
          disabled={!loaded || saving || loaded.people.length === 0}
        >
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={saving}>
          {saved ? "Close" : "Cancel"}
        </Button>
      </div>
    </div>
  );
}
