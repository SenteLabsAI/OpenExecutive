"use client";

import { useMemo, useState } from "react";
import Icon from "@/components/Icon";
import type { SessionSummary } from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import { CHANNEL_LABELS, sessionChannel, sessionTitle } from "@/lib/sessionChannel";
import { groupSessionsByDate, type GroupKey } from "@/lib/sessionGroups";

const GROUP_CAP = 20;
const DEFAULT_COLLAPSED = new Set<GroupKey>(["older"]);

interface ChatHistoryProps {
  /** Already filtered by channel and search; newest first. */
  sessions: SessionSummary[];
  /** While a search is active every group is forced open so no match is hidden. */
  searching: boolean;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
}

// Date-grouped conversation list for /chats — the full history the sidebar's
// short Recent list links to.
export default function ChatHistory({ sessions, searching, onSelect, onDelete }: ChatHistoryProps) {
  const [collapsedOverride, setCollapsedOverride] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Set<string>>(new Set());

  const groups = useMemo(() => groupSessionsByDate(sessions), [sessions]);

  if (groups.length === 0) {
    return <p className="px-3 py-6 text-sm text-fg-muted">No conversations match.</p>;
  }

  // Stored collapse state (default-collapsed unless the user toggled it). While
  // searching we force every group open so matches are never hidden, but the
  // stored state is preserved and re-applies once the query is cleared.
  const storedCollapsed = (key: GroupKey) =>
    key in collapsedOverride ? collapsedOverride[key] : DEFAULT_COLLAPSED.has(key);
  const isCollapsed = (key: GroupKey) => (searching ? false : storedCollapsed(key));
  const toggleCollapse = (key: GroupKey) =>
    setCollapsedOverride((prev) => ({ ...prev, [key]: !storedCollapsed(key) }));
  const revealAll = (key: GroupKey) => setShowAll((prev) => new Set(prev).add(key));

  return (
    <div className="space-y-4">
      {groups.map((group) => {
        const collapsed = isCollapsed(group.key);
        const expandedAll = searching || showAll.has(group.key);
        const visible = expandedAll ? group.items : group.items.slice(0, GROUP_CAP);
        const hiddenCount = group.items.length - visible.length;
        return (
          <section key={group.key}>
            <button
              type="button"
              onClick={() => toggleCollapse(group.key)}
              aria-expanded={!collapsed}
              className="w-full flex items-center gap-1.5 px-3 py-1.5 text-fg-subtle hover:text-fg transition-colors cursor-pointer"
            >
              <Icon
                name="chevron-right"
                size="w-3 h-3"
                className={`transition-transform ${collapsed ? "" : "rotate-90"}`}
              />
              <span className="text-[11px] font-medium uppercase tracking-wide">{group.label}</span>
              <span className="text-[10px] text-fg-subtle/70">{group.items.length}</span>
            </button>
            {!collapsed && (
              <div className="mt-1 space-y-0.5">
                {visible.map((s) => (
                  <ChatRow key={s.session_id} session={s} onSelect={onSelect} onDelete={onDelete} />
                ))}
                {hiddenCount > 0 && (
                  <button
                    type="button"
                    onClick={() => revealAll(group.key)}
                    className="w-full text-left px-3 py-1.5 text-xs text-fg-subtle hover:text-fg cursor-pointer"
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

function ChatRow({
  session,
  onSelect,
  onDelete,
}: {
  session: SessionSummary;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
}) {
  const channel = sessionChannel(session.session_id);
  const messages = `${session.message_count} message${session.message_count === 1 ? "" : "s"}`;
  return (
    <div className="group flex items-center gap-2 rounded-lg hover:bg-surface-elevated/60 transition-colors">
      <button
        type="button"
        onClick={() => onSelect(session.session_id)}
        className="flex-1 min-w-0 text-left px-3 py-2.5 cursor-pointer"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-sm text-fg truncate">{sessionTitle(session)}</span>
          {channel !== "web" && (
            <span className="flex-shrink-0 text-[10px] font-medium uppercase tracking-wide text-fg-muted ring-1 ring-line rounded px-1.5 py-0.5 leading-none">
              {CHANNEL_LABELS[channel]}
            </span>
          )}
        </span>
        <span className="block text-xs text-fg-subtle mt-0.5">
          {formatRelativeTime(session.updated_at)} · {messages}
        </span>
      </button>
      {/* Always visible on touch (no hover); revealed on hover/focus on desktop. */}
      <button
        type="button"
        aria-label="Delete chat"
        title="Delete chat"
        onClick={() => onDelete(session.session_id)}
        className="mr-1 min-h-touch min-w-touch flex items-center justify-center rounded-md text-fg-muted hover:text-red-400 hover:bg-red-500/10 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 focus:opacity-100 transition-opacity cursor-pointer"
      >
        <Icon name="trash" size="w-4 h-4" />
      </button>
    </div>
  );
}
