"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { formatDate } from "@/components/memories/shared";
import SettingsCard from "@/components/settings/SettingsCard";
import {
  listPeopleMemory,
  listPersonConclusions,
  type PeopleMemory,
  type PersonConclusion,
  type PersonMemory,
} from "@/lib/api";
import { cardEntry, noteAboutYou } from "@/lib/aboutYou";

// Settings → About you: what peer memory has learned about the signed-in
// person, derived server-side from their conversations. GET /memories/people
// returns only the caller's own entry (the principal's included), so this is
// one person's profile and notes. Read-only: it is not the Executive's own
// record to edit. Renders nothing when peer memory is off.

const NOTHING_YET =
  "Nothing learned about you yet. It fills in as you talk with the Executive.";

// Profile lines past this many fold behind a toggle.
const CARD_PREVIEW_LINES = 4;
// Load the next page once the notes pane is scrolled within this many pixels
// of its bottom.
const NOTES_SCROLL_SLACK_PX = 48;

export default function AboutYouCard() {
  const [data, setData] = useState<PeopleMemory | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listPeopleMemory()
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch(() => {
        // The read crosses to another service: an error must not read as
        // "nothing known".
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (data?.status === "disabled") return null;

  const me = data?.status === "ok" ? data.people[0] : undefined;
  const learned = me?.last_observed_at ? `Last learned ${formatDate(me.last_observed_at)}.` : null;

  return (
    <SettingsCard
      title="Your profile"
      description={["Learned from your chats and replies. Only you see this.", learned].filter(Boolean).join(" ")}
    >
      {loading ? (
        <p className="text-[15px] text-fg-muted">Loading…</p>
      ) : failed || !data || data.status === "error" || me?.error ? (
        <p className="text-[15px] text-fg-muted">Couldn&apos;t load what it knows about you right now.</p>
      ) : !me || (me.card.length === 0 && me.conclusion_count === 0) ? (
        <p className="text-[15px] text-fg-muted">{NOTHING_YET}</p>
      ) : (
        <div className="space-y-5">
          {me.card.length > 0 && <Profile lines={me.card} />}
          {me.recent.length > 0 && <Notes item={me} />}
        </div>
      )}
    </SettingsCard>
  );
}

function Profile({ lines }: { lines: string[] }) {
  const [open, setOpen] = useState(false);
  const hidden = lines.length - CARD_PREVIEW_LINES;
  const shown = open || hidden <= 0 ? lines : lines.slice(0, CARD_PREVIEW_LINES);
  return (
    <div>
      <div className="grid grid-cols-[5.5rem_1fr] gap-x-3 gap-y-2 text-[15px]">
        {shown.map((line, i) => {
          const entry = cardEntry(line);
          return entry.label ? (
            <div key={i} className="contents">
              <div className="text-fg-muted break-words">{entry.label}</div>
              <div className="text-fg break-words min-w-0">{entry.value}</div>
            </div>
          ) : (
            <div key={i} className="col-span-2 text-fg break-words min-w-0">
              {entry.value}
            </div>
          );
        })}
      </div>
      {hidden > 0 && (
        <button
          onClick={() => setOpen((o) => !o)}
          className="mt-1 min-h-touch text-[15px] font-medium text-accent hover:underline"
        >
          {open ? "Show less" : `Show ${hidden} more`}
        </button>
      )}
    </div>
  );
}

/** Newest first. Seeds from the overview's few; "Show all" then reads the
 * full list page by page as the pane scrolls. */
function Notes({ item }: { item: PersonMemory }) {
  const [notes, setNotes] = useState<PersonConclusion[]>(item.recent);
  const [nextPage, setNextPage] = useState<number | null>(null); // null until "Show all"
  const [hasMore, setHasMore] = useState(item.conclusion_count > item.recent.length);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const loadingRef = useRef(false);
  const paneRef = useRef<HTMLDivElement>(null);
  const load = useCallback(
    async (page: number) => {
      if (loadingRef.current) return;
      loadingRef.current = true;
      setLoading(true);
      setFailed(false);
      try {
        const res = await listPersonConclusions(item.person_id, page);
        if (res.status !== "ok") throw new Error(res.status);
        setNotes((prev) => {
          // Page 1 already holds the seeded few; later pages append. New notes
          // landing between reads shift the pages, so drop repeats.
          const base = page === 1 ? [] : prev;
          const seen = new Set(base.map((c) => `${c.created_at}|${c.content}`));
          return [...base, ...res.items.filter((c) => !seen.has(`${c.created_at}|${c.content}`))];
        });
        setHasMore(res.has_more);
        setNextPage(page + 1);
      } catch {
        setFailed(true);
      } finally {
        loadingRef.current = false;
        setLoading(false);
      }
    },
    [item.person_id],
  );

  const onScroll = () => {
    const el = paneRef.current;
    if (!el || nextPage === null || !hasMore || failed) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < NOTES_SCROLL_SLACK_PX) void load(nextPage);
  };

  // A page too short to scroll can never fire onScroll: keep filling until it can.
  useEffect(() => {
    const el = paneRef.current;
    if (!el || nextPage === null || !hasMore || loading || failed) return;
    if (el.scrollHeight <= el.clientHeight) void load(nextPage);
  }, [notes, nextPage, hasMore, loading, failed, load]);

  const remaining = Math.max(item.conclusion_count - notes.length, 0);
  return (
    <div>
      <h3 className="text-[13px] font-semibold uppercase tracking-wide text-fg-subtle mb-2">
        Notes · {nextPage !== null ? `${notes.length} of ` : ""}
        {Math.max(item.conclusion_count, notes.length)}
      </h3>
      <div
        ref={paneRef}
        onScroll={onScroll}
        className="max-h-80 overflow-y-auto rounded-md border border-line/60 bg-surface-elevated/40"
      >
        <ul className="divide-y divide-line/60">
          {notes.map((c, i) => (
            <li key={`${c.created_at}-${i}`} className="grid grid-cols-1 gap-0.5 px-3 py-2.5 sm:grid-cols-[5.5rem_1fr] sm:gap-3">
              <span className="text-[11px] text-fg-subtle tabular-nums pt-0.5">{formatDate(c.created_at)}</span>
              <span className="text-[15px] text-fg leading-relaxed break-words">
                {noteAboutYou(c.content, item.person_id, item.full_name)}
              </span>
            </li>
          ))}
        </ul>
        {(loading || failed || (hasMore && nextPage === null)) && (
          <div className="px-3 py-2 text-sm text-fg-muted border-t border-line/60">
            {loading ? (
              "Loading…"
            ) : failed ? (
              <button onClick={() => void load(nextPage ?? 1)} className="min-h-touch hover:text-fg">
                Couldn&apos;t load more — retry
              </button>
            ) : (
              <button onClick={() => void load(1)} className="min-h-touch font-medium text-accent hover:underline">
                Show all {item.conclusion_count} notes ({remaining} more)
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
