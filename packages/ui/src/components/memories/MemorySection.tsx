"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteAdvice,
  deleteDecision,
  deleteInitiative,
  listAdvice,
  listDecisions,
  listInitiatives,
  listPeopleMemory,
  listPersonConclusions,
  updateAdvice,
  updateDecision,
  updateInitiative,
  type Advice,
  type Decision,
  type Initiative,
  type PeopleMemory,
  type PersonConclusion,
  type PersonMemory,
} from "@/lib/api";
import { DOMAINS, STATUSES, EmptyState, formatDate } from "./shared";

type MemoryTab = "decisions" | "initiatives" | "advice" | "people";

const MEMORY_EMPTY = "No memories yet — they're extracted automatically after chats.";
const PEOPLE_EMPTY =
  "Nothing learned about anyone yet — peer memory fills in as people talk with the Executive.";
const PEOPLE_UNAVAILABLE = "Peer memory is unavailable right now.";

// ---------------------------------------------------------------------------
// Section shell — the "what it knows" half of the Pulse page.
// ---------------------------------------------------------------------------

export default function MemorySection() {
  const [tab, setTab] = useState<MemoryTab>("decisions");
  // Each tab reports its row count so the tab labels can carry a live badge.
  // All three tabs stay mounted (inactive ones hidden) so every count loads up
  // front; a tab's own edit/delete re-runs its refresh, which reports the new
  // length back here, keeping that tab's badge correct.
  const [counts, setCounts] = useState<Record<MemoryTab, number | null>>({
    decisions: null,
    initiatives: null,
    advice: null,
    people: null,
  });
  // Peer memory is optional: until its status is known the People tab shows
  // (so the bar does not jump on installs that have it); once the backend says
  // "disabled" the tab goes away for good.
  const [peopleEnabled, setPeopleEnabled] = useState<boolean | null>(null);
  // Stable per-tab callbacks — these are passed to the (always-mounted) tabs as
  // `onCount`, which lives in each tab's `refresh` useCallback deps. They MUST
  // keep a constant identity across renders, or the tab's refresh→useEffect
  // chain would re-fire every render and loop forever. (Do NOT inline a
  // `setCount(tab)` factory here.)
  const onCountDecisions = useCallback((n: number) => setCounts((c) => ({ ...c, decisions: n })), []);
  const onCountInitiatives = useCallback((n: number) => setCounts((c) => ({ ...c, initiatives: n })), []);
  const onCountAdvice = useCallback((n: number) => setCounts((c) => ({ ...c, advice: n })), []);
  const onCountPeople = useCallback(
    (n: number | null) => setCounts((c) => ({ ...c, people: n })),
    [],
  );
  const onPeopleStatus = useCallback(
    (s: PeopleMemory["status"]) => setPeopleEnabled(s !== "disabled"),
    [],
  );

  useEffect(() => {
    if (peopleEnabled === false && tab === "people") setTab("decisions");
  }, [peopleEnabled, tab]);

  const tabs: MemoryTab[] =
    peopleEnabled === false
      ? ["decisions", "initiatives", "advice"]
      : ["decisions", "initiatives", "advice", "people"];

  return (
    <div className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="mb-3">
        <div className="flex gap-1 p-1 bg-surface-overlay/60 rounded-xl w-fit border border-line-strong/50">
          {tabs.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors capitalize ${
                tab === t
                  ? "bg-surface-input text-fg shadow-sm"
                  : "text-fg-muted hover:text-fg"
              }`}
            >
              {t}
              {counts[t] != null && (
                <span className="ml-1.5 text-xs font-normal tabular-nums text-fg-subtle">
                  {counts[t]}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* One scroll region shared by all (always-mounted) tabs, mirroring
          the Recent activity card — the list scrolls instead of growing. */}
      <div className="max-h-[32rem] overflow-y-auto pr-1">
        <div className={tab === "decisions" ? "" : "hidden"}>
          <DecisionsTab onCount={onCountDecisions} />
        </div>
        <div className={tab === "initiatives" ? "" : "hidden"}>
          <InitiativesTab onCount={onCountInitiatives} />
        </div>
        <div className={tab === "advice" ? "" : "hidden"}>
          <AdviceTab onCount={onCountAdvice} />
        </div>
        <div className={tab === "people" ? "" : "hidden"}>
          <PeopleTab onCount={onCountPeople} onStatus={onPeopleStatus} />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------

function DecisionsTab({ onCount }: { onCount: (n: number) => void }) {
  const [items, setItems] = useState<Decision[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await listDecisions();
      setItems(rows);
      onCount(rows.length);
    } finally {
      setLoading(false);
    }
  }, [onCount]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDelete = useCallback(async (id: number) => {
    if (!window.confirm("Delete this memory? This cannot be undone.")) return;
    try {
      await deleteDecision(id);
    } catch {
      window.alert("Failed to delete.");
      return;
    }
    void refresh();
  }, [refresh]);

  if (loading) return <div className="text-fg-muted text-sm">Loading…</div>;
  if (items.length === 0) return <EmptyState message={MEMORY_EMPTY} />;

  return (
    <div className="divide-y divide-line">
      {items.map((d) => (
        <DecisionRow
          key={d.id}
          decision={d}
          editing={editingId === d.id}
          onEdit={() => setEditingId(d.id)}
          onCancel={() => setEditingId(null)}
          onSave={async (patch) => {
            try {
              await updateDecision(d.id, patch);
            } catch {
              window.alert("Failed to save.");
              return;
            }
            setEditingId(null);
            void refresh();
          }}
          onDelete={() => handleDelete(d.id)}
        />
      ))}
    </div>
  );
}

function DecisionRow({
  decision,
  editing,
  onEdit,
  onCancel,
  onSave,
  onDelete,
}: {
  decision: Decision;
  editing: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: (patch: Partial<Decision>) => void;
  onDelete: () => void;
}) {
  const [domain, setDomain] = useState(decision.domain);
  const [summary, setSummary] = useState(decision.summary);
  const [rationale, setRationale] = useState(decision.rationale);
  const [outcome, setOutcome] = useState(decision.outcome);
  const [tags, setTags] = useState(decision.tags);

  useEffect(() => {
    if (editing) {
      setDomain(decision.domain);
      setSummary(decision.summary);
      setRationale(decision.rationale);
      setOutcome(decision.outcome);
      setTags(decision.tags);
    }
  }, [editing, decision]);

  return (
    <div className="group py-3 hover:bg-surface-overlay/30 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 text-xs text-fg-muted">
          <span className="px-2 py-0.5 rounded bg-surface-overlay text-fg font-medium">{editing ? domain : decision.domain}</span>
          <span>{formatDate(decision.timestamp)}</span>
        </div>
        {!editing && (
          <div className="flex gap-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <button onClick={onEdit} className="text-xs text-fg-muted hover:text-fg">Edit</button>
            <button onClick={onDelete} className="text-xs text-red-400 hover:text-red-300">Delete</button>
          </div>
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <select
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          >
            {DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input
            type="text"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Summary"
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <textarea
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            placeholder="Rationale"
            rows={2}
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <input
            type="text"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
            placeholder="Outcome"
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <input
            type="text"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="Tags"
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <div className="flex gap-2 justify-end">
            <button onClick={onCancel} className="text-xs px-3 py-1.5 rounded text-fg-muted hover:text-fg">Cancel</button>
            <button
              onClick={() => onSave({ domain, summary, rationale, outcome, tags })}
              className="text-xs px-3 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white"
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="text-sm text-fg line-clamp-2" title={decision.summary}>{decision.summary}</div>
          {decision.rationale && <div className="text-xs text-fg-muted line-clamp-2" title={`Rationale: ${decision.rationale}`}><span className="text-fg-muted">Rationale: </span>{decision.rationale}</div>}
          {decision.outcome && <div className="text-xs text-fg-muted line-clamp-2" title={`Outcome: ${decision.outcome}`}><span className="text-fg-muted">Outcome: </span>{decision.outcome}</div>}
          {decision.tags && <div className="text-xs text-fg-muted truncate" title={`Tags: ${decision.tags}`}>Tags: {decision.tags}</div>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Initiatives
// ---------------------------------------------------------------------------

function InitiativesTab({ onCount }: { onCount: (n: number) => void }) {
  const [items, setItems] = useState<Initiative[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await listInitiatives();
      setItems(rows);
      onCount(rows.length);
    } finally {
      setLoading(false);
    }
  }, [onCount]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDelete = useCallback(async (id: number) => {
    if (!window.confirm("Delete this memory? This cannot be undone.")) return;
    try {
      await deleteInitiative(id);
    } catch {
      window.alert("Failed to delete.");
      return;
    }
    void refresh();
  }, [refresh]);

  if (loading) return <div className="text-fg-muted text-sm">Loading…</div>;
  if (items.length === 0) return <EmptyState message={MEMORY_EMPTY} />;

  return (
    <div className="divide-y divide-line">
      {items.map((it) => (
        <InitiativeRow
          key={it.id}
          initiative={it}
          editing={editingId === it.id}
          onEdit={() => setEditingId(it.id)}
          onCancel={() => setEditingId(null)}
          onSave={async (patch) => {
            try {
              await updateInitiative(it.id, patch);
            } catch {
              window.alert("Failed to save.");
              return;
            }
            setEditingId(null);
            void refresh();
          }}
          onDelete={() => handleDelete(it.id)}
        />
      ))}
    </div>
  );
}

function InitiativeRow({
  initiative,
  editing,
  onEdit,
  onCancel,
  onSave,
  onDelete,
}: {
  initiative: Initiative;
  editing: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: (patch: Partial<Initiative>) => void;
  onDelete: () => void;
}) {
  const [title, setTitle] = useState(initiative.title);
  const [status, setStatus] = useState(initiative.status);
  const [summary, setSummary] = useState(initiative.summary);

  useEffect(() => {
    if (editing) {
      setTitle(initiative.title);
      setStatus(initiative.status);
      setSummary(initiative.summary);
    }
  }, [editing, initiative]);

  return (
    <div className="group py-3 hover:bg-surface-overlay/30 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 text-xs text-fg-muted">
          <span className="px-2 py-0.5 rounded bg-surface-overlay text-fg font-medium capitalize">{editing ? status : initiative.status}</span>
          <span>updated {formatDate(initiative.updated_at)}</span>
        </div>
        {!editing && (
          <div className="flex gap-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <button onClick={onEdit} className="text-xs text-fg-muted hover:text-fg">Edit</button>
            <button onClick={onDelete} className="text-xs text-red-400 hover:text-red-300">Delete</button>
          </div>
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          >
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <textarea
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Summary"
            rows={2}
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <div className="flex gap-2 justify-end">
            <button onClick={onCancel} className="text-xs px-3 py-1.5 rounded text-fg-muted hover:text-fg">Cancel</button>
            <button
              onClick={() => onSave({ title, status, summary })}
              className="text-xs px-3 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white"
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="text-sm text-fg font-medium line-clamp-2" title={initiative.title}>{initiative.title}</div>
          {initiative.summary && <div className="text-xs text-fg-muted line-clamp-2" title={initiative.summary}>{initiative.summary}</div>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Advice
// ---------------------------------------------------------------------------

function AdviceTab({ onCount }: { onCount: (n: number) => void }) {
  const [items, setItems] = useState<Advice[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await listAdvice();
      setItems(rows);
      onCount(rows.length);
    } finally {
      setLoading(false);
    }
  }, [onCount]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDelete = useCallback(async (id: number) => {
    if (!window.confirm("Delete this memory? This cannot be undone.")) return;
    try {
      await deleteAdvice(id);
    } catch {
      window.alert("Failed to delete.");
      return;
    }
    void refresh();
  }, [refresh]);

  if (loading) return <div className="text-fg-muted text-sm">Loading…</div>;
  if (items.length === 0) return <EmptyState message={MEMORY_EMPTY} />;

  return (
    <div className="divide-y divide-line">
      {items.map((a) => (
        <AdviceRow
          key={a.id}
          advice={a}
          editing={editingId === a.id}
          onEdit={() => setEditingId(a.id)}
          onCancel={() => setEditingId(null)}
          onSave={async (patch) => {
            try {
              await updateAdvice(a.id, patch);
            } catch {
              window.alert("Failed to save.");
              return;
            }
            setEditingId(null);
            void refresh();
          }}
          onDelete={() => handleDelete(a.id)}
        />
      ))}
    </div>
  );
}

function AdviceRow({
  advice,
  editing,
  onEdit,
  onCancel,
  onSave,
  onDelete,
}: {
  advice: Advice;
  editing: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: (patch: Partial<Advice>) => void;
  onDelete: () => void;
}) {
  const [domain, setDomain] = useState(advice.domain);
  const [querySummary, setQuerySummary] = useState(advice.query_summary);
  const [adviceSummary, setAdviceSummary] = useState(advice.advice_summary);

  useEffect(() => {
    if (editing) {
      setDomain(advice.domain);
      setQuerySummary(advice.query_summary);
      setAdviceSummary(advice.advice_summary);
    }
  }, [editing, advice]);

  return (
    <div className="group py-3 hover:bg-surface-overlay/30 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 text-xs text-fg-muted">
          <span className="px-2 py-0.5 rounded bg-surface-overlay text-fg font-medium">{editing ? domain : advice.domain}</span>
          <span>{formatDate(advice.timestamp)}</span>
        </div>
        {!editing && (
          <div className="flex gap-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <button onClick={onEdit} className="text-xs text-fg-muted hover:text-fg">Edit</button>
            <button onClick={onDelete} className="text-xs text-red-400 hover:text-red-300">Delete</button>
          </div>
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <select
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          >
            {DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input
            type="text"
            value={querySummary}
            onChange={(e) => setQuerySummary(e.target.value)}
            placeholder="What the user asked"
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <textarea
            value={adviceSummary}
            onChange={(e) => setAdviceSummary(e.target.value)}
            placeholder="Advice given"
            rows={3}
            className="w-full bg-surface border border-line rounded px-2 py-1.5 text-sm text-fg"
          />
          <div className="flex gap-2 justify-end">
            <button onClick={onCancel} className="text-xs px-3 py-1.5 rounded text-fg-muted hover:text-fg">Cancel</button>
            <button
              onClick={() => onSave({ domain, query_summary: querySummary, advice_summary: adviceSummary })}
              className="text-xs px-3 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white"
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="text-xs text-fg-muted line-clamp-2" title={`Q: ${advice.query_summary}`}>Q: {advice.query_summary}</div>
          <div className="text-sm text-fg line-clamp-2" title={advice.advice_summary}>{advice.advice_summary}</div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// People — peer memory. What the Executive has learned about each person,
// derived server-side from their conversations. Read-only: unlike the three
// lists above this is not the Executive's own record to edit, and the header
// counts its notes alongside them.
// ---------------------------------------------------------------------------

function PeopleTab({
  onCount,
  onStatus,
}: {
  onCount: (n: number | null) => void;
  onStatus: (s: PeopleMemory["status"]) => void;
}) {
  const [data, setData] = useState<PeopleMemory | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const res = await listPeopleMemory();
      setData(res);
      onStatus(res.status);
      // The badge counts people, not notes (the header carries the notes).
      // Unavailable is not zero: leave the badge blank rather than claim 0.
      onCount(res.status === "ok" ? res.people.length : null);
    } catch {
      // Unlike the other tabs, an error here must not masquerade as "nothing
      // known": the read crosses to another service.
      setFailed(true);
      onCount(null);
    } finally {
      setLoading(false);
    }
  }, [onCount, onStatus]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (loading) return <div className="text-fg-muted text-sm">Loading…</div>;
  if (failed || !data || data.status === "error") {
    return <div className="text-fg-muted text-sm">{PEOPLE_UNAVAILABLE}</div>;
  }
  if (data.status === "disabled") return null;
  if (data.people.length === 0) return <EmptyState message={PEOPLE_EMPTY} />;

  return (
    <div className="space-y-3">
      {data.people.map((p) => (
        <PersonMemoryRow key={p.person_id} item={p} />
      ))}
    </div>
  );
}

// Card lines past this many fold behind a toggle so one talkative card
// doesn't push everyone else off the screen.
const CARD_PREVIEW_LINES = 4;
// Load the next page once the notes pane is scrolled within this many pixels
// of its bottom.
const NOTES_SCROLL_SLACK_PX = 48;

function PersonMemoryRow({ item }: { item: PersonMemory }) {
  const notes = `${item.conclusion_count} ${item.conclusion_count === 1 ? "note" : "notes"}`;
  const learned = item.last_observed_at ? ` · learned ${formatDate(item.last_observed_at)}` : "";
  const nothingYet = !item.error && item.conclusion_count === 0 && item.card.length === 0;
  return (
    <div className="rounded-lg border border-line bg-surface-overlay/30 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-semibold text-fg truncate">{item.full_name}</span>
          {item.is_principal && (
            <span className="inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-violet-500/20 text-violet-300 border-violet-500/30">
              Principal
            </span>
          )}
          {/* The notes below name people by their bare peer id; this is the key. */}
          <span className="text-[10px] text-fg-subtle shrink-0">peer {item.person_id}</span>
        </div>
        <div className="text-xs text-fg-muted tabular-nums shrink-0">
          {item.error ? "couldn't read" : `${notes}${learned}`}
        </div>
      </div>
      {item.card.length > 0 && <PersonCard lines={item.card} />}
      {item.recent.length > 0 && <PersonNotes item={item} />}
      {nothingYet && <div className="mt-2 text-xs text-fg-subtle">Nothing learned yet.</div>}
    </div>
  );
}

function PersonCard({ lines }: { lines: string[] }) {
  const [open, setOpen] = useState(false);
  const hidden = lines.length - CARD_PREVIEW_LINES;
  const shown = open || hidden <= 0 ? lines : lines.slice(0, CARD_PREVIEW_LINES);
  return (
    <div className="mt-3">
      <div className="text-[10px] uppercase tracking-wide text-fg-subtle mb-1">Profile</div>
      <ul className="space-y-0.5 text-xs text-fg-muted list-disc pl-4 marker:text-fg-subtle">
        {shown.map((line, i) => (
          <li key={i} className="break-words">{line}</li>
        ))}
      </ul>
      {hidden > 0 && (
        <button onClick={() => setOpen((o) => !o)} className="mt-1 text-xs text-fg-muted hover:text-fg">
          {open ? "Show less" : `Show ${hidden} more`}
        </button>
      )}
    </div>
  );
}

/** Newest first. Seeds from the overview's few; "Show all" then reads the
 * full list page by page as the pane scrolls. */
function PersonNotes({ item }: { item: PersonMemory }) {
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
    <div className="mt-3">
      <div className="text-[10px] uppercase tracking-wide text-fg-subtle mb-1">
        Notes{nextPage !== null && ` · ${notes.length} of ${Math.max(item.conclusion_count, notes.length)}`}
      </div>
      <div
        ref={paneRef}
        onScroll={onScroll}
        className="max-h-80 overflow-y-auto rounded-md border border-line/60 bg-surface-elevated/40"
      >
        <ul className="divide-y divide-line/60">
          {notes.map((c, i) => (
            <li key={`${c.created_at}-${i}`} className="grid grid-cols-[5.5rem_1fr] gap-3 px-3 py-2">
              <span className="text-[11px] text-fg-subtle tabular-nums pt-0.5">{formatDate(c.created_at)}</span>
              <span className="text-sm text-fg leading-relaxed break-words">{c.content}</span>
            </li>
          ))}
        </ul>
        {(loading || failed || (hasMore && nextPage === null)) && (
          <div className="px-3 py-2 text-xs text-fg-muted border-t border-line/60">
            {loading ? (
              "Loading…"
            ) : failed ? (
              <button onClick={() => void load(nextPage ?? 1)} className="hover:text-fg">
                Couldn&apos;t load more — retry
              </button>
            ) : (
              <button onClick={() => void load(1)} className="hover:text-fg">
                Show all {item.conclusion_count} notes ({remaining} more)
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
