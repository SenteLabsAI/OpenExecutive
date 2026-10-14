"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Icon from "@/components/Icon";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import {
  getPeopleViewer,
  getSessionShares,
  listPeople,
  setSessionShares,
  updateSession,
  type Person,
  type SessionSummary,
} from "@/lib/api";
import { downloadChat } from "@/lib/downloadChat";
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
  /** Reloads the list after a rename or a pin. */
  onChanged: () => void;
}

// Date-grouped conversation list for /chats — the full history the sidebar's
// short Recent list links to.
export default function ChatHistory({ sessions, searching, query = "", onSelect, onDelete, onChanged }: ChatHistoryProps) {
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
                  <ChatRow
                    key={s.session_id}
                    session={s}
                    query={query}
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

type RowPanel = "delete" | "rename" | "share" | null;

export function ChatRow({
  session,
  query = "",
  onSelect,
  onDelete,
  onChanged,
}: {
  session: SessionSummary;
  query?: string;
  onSelect: (sessionId: string) => void;
  /** Absent for a chat shared with the viewer, which only they can't delete. */
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
  const readOnly = !!session.read_only;
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

  const menu = readOnly
    ? [{ label: "Download", onSelect: () => void download() }]
    : [
        { label: session.pinned_at ? "Unpin" : "Pin", onSelect: () => void togglePin() },
        { label: "Rename", onSelect: () => open("rename") },
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
            {channel !== "web" && (
              <span className="flex-shrink-0 text-[11px] font-medium uppercase tracking-wide text-fg-muted ring-1 ring-line rounded-md px-1.5 py-0.5 leading-none">
                {CHANNEL_LABELS[channel]}
              </span>
            )}
          </span>
          <span className="block text-sm text-fg-subtle mt-0.5">
            {readOnly ? (
              <>
                Shared by {session.shared_by ?? "someone on your team"} ·{" "}
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

interface ShareState {
  people: Person[];
  chosen: Set<number>;
}

// Who the chat is shared with, to read: team members only, never the owner.
// Loaded when the panel opens (it mounts then), keyed by the chat it is for.
function SharePanel({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't share this chat.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border-t border-line px-4 py-3 space-y-3">
      <p className="text-sm text-fg">Share this chat to read with:</p>
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
