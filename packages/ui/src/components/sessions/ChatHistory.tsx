"use client";

import { useMemo, useState } from "react";
import Icon from "@/components/Icon";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import type { SessionSummary } from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";
import { highlightParts, matchLabel } from "@/lib/chatSearch";
import { CHANNEL_LABELS, sessionChannel, sessionTitle } from "@/lib/sessionChannel";
import { groupSessionsByDate, type GroupKey } from "@/lib/sessionGroups";

const GROUP_CAP = 20;
const DEFAULT_COLLAPSED = new Set<GroupKey>(["older"]);

interface ChatHistoryProps {
  /** Already filtered by channel and search; newest first. */
  sessions: SessionSummary[];
  /** While a search is active every group is forced open so no match is hidden. */
  searching: boolean;
  /** The words the server searched for, to mark in each result's snippet. */
  query?: string;
  onSelect: (sessionId: string) => void;
  /** Deletes after the row has asked; resolves false when it failed. */
  onDelete: (sessionId: string) => Promise<boolean>;
}

// Date-grouped conversation list for /chats — the full history the sidebar's
// short Recent list links to.
export default function ChatHistory({ sessions, searching, query = "", onSelect, onDelete }: ChatHistoryProps) {
  const [collapsedOverride, setCollapsedOverride] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Set<string>>(new Set());

  const groups = useMemo(() => groupSessionsByDate(sessions), [sessions]);

  if (groups.length === 0) {
    return <p className="px-1 py-6 text-[15px] text-fg-muted">No conversations match.</p>;
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
              className="w-full flex items-center gap-2 min-h-[40px] px-1 text-fg-muted hover:text-fg transition-colors cursor-pointer"
            >
              <Icon
                name="chevron-right"
                size="w-3.5 h-3.5"
                className={`transition-transform ${collapsed ? "" : "rotate-90"}`}
              />
              <span className="text-sm font-semibold">{group.label}</span>
              <span className="text-sm text-fg-subtle">{group.items.length}</span>
            </button>
            {!collapsed && (
              <div className="mt-1 space-y-2">
                {visible.map((s) => (
                  <ChatRow key={s.session_id} session={s} query={query} onSelect={onSelect} onDelete={onDelete} />
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

function ChatRow({
  session,
  query,
  onSelect,
  onDelete,
}: {
  session: SessionSummary;
  query: string;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => Promise<boolean>;
}) {
  // Delete is in the row's ⋯ menu and asks here, in the row, first.
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [failed, setFailed] = useState(false);
  const channel = sessionChannel(session.session_id);
  const messages =
    matchLabel(session.match_count) ??
    `${session.message_count} message${session.message_count === 1 ? "" : "s"}`;

  const confirmDelete = async () => {
    setDeleting(true);
    setFailed(false);
    const ok = await onDelete(session.session_id);
    // On success the row leaves the list; on failure it stays and says so.
    if (!ok) {
      setDeleting(false);
      setFailed(true);
    }
  };

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
            {channel !== "web" && (
              <span className="flex-shrink-0 text-[11px] font-medium uppercase tracking-wide text-fg-muted ring-1 ring-line rounded-md px-1.5 py-0.5 leading-none">
                {CHANNEL_LABELS[channel]}
              </span>
            )}
          </span>
          <span className="block text-sm text-fg-subtle mt-0.5">
            {formatRelativeTime(session.updated_at)} · {messages}
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
        <OverflowMenu
          label="More actions for this chat"
          items={[{ label: "Delete chat", danger: true, onSelect: () => setConfirming(true) }]}
        />
      </div>
      {(confirming || failed) && (
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
              setConfirming(false);
              setFailed(false);
            }}
            disabled={deleting}
          >
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
