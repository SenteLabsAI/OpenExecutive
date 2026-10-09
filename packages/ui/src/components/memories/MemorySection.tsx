"use client";

import { useCallback, useEffect, useState } from "react";
import {
  deleteAdvice,
  deleteDecision,
  deleteInitiative,
  listAdvice,
  listDecisions,
  listInitiatives,
  updateAdvice,
  updateDecision,
  updateInitiative,
  type Advice,
  type Decision,
  type Initiative,
} from "@/lib/api";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import CorrectionsTab from "./CorrectionsTab";
import HistoryTab from "./HistoryTab";
import type { MemoryView } from "@/lib/pulseView";
import { DOMAINS, STATUSES, EmptyState, formatDate } from "./shared";

const MEMORY_EMPTY = "No memories yet — they're extracted automatically after chats.";

// Counts are shown on the Pulse overview's tiles, not here.
const ignoreCount = () => {};

// ---------------------------------------------------------------------------
// One memory list, on its own screen behind a Pulse tile.
// ---------------------------------------------------------------------------

export default function MemoryList({ view }: { view: MemoryView }) {
  // History (Always in the loop) is the signed-in person's own notes; someone
  // with none to see (not signed in, not on the roster) gets a plain message.
  const [historyAvailable, setHistoryAvailable] = useState(true);
  const onHistoryAvailable = useCallback((available: boolean) => setHistoryAvailable(available), []);

  return (
    <div className="rounded-2xl border border-line bg-surface-elevated px-4 sm:px-5 py-1">
      {view === "decisions" && <DecisionsTab onCount={ignoreCount} />}
      {view === "initiatives" && <InitiativesTab onCount={ignoreCount} />}
      {view === "advice" && <AdviceTab onCount={ignoreCount} />}
      {view === "corrections" && <CorrectionsTab onCount={ignoreCount} />}
      {view === "history" &&
        (historyAvailable ? (
          <HistoryTab onCount={ignoreCount} onAvailable={onHistoryAvailable} />
        ) : (
          <EmptyState message="There are no notes for you here. They're kept for people on the People list, once they're signed in." />
        ))}
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

  if (loading) return <div className="text-fg-muted text-[15px] py-4">Loading…</div>;
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
    <div className="py-3.5">
      <div className="flex items-center justify-between gap-3 mb-1">
        <div className="flex items-center gap-2 text-sm text-fg-muted">
          <span className="px-2 py-0.5 rounded-lg bg-surface-overlay text-fg font-medium capitalize">{editing ? domain : decision.domain}</span>
          <span>{formatDate(decision.timestamp)}</span>
        </div>
        {!editing && (
          <OverflowMenu
            size="sm"
            label="Memory actions"
            items={[
              { label: "Edit", onSelect: onEdit },
              { label: "Delete", danger: true, onSelect: onDelete },
            ]}
          />
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <select
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input
            type="text"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Summary"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <textarea
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            placeholder="Rationale"
            rows={2}
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <input
            type="text"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
            placeholder="Outcome"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <input
            type="text"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="Tags"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <div className="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" className="!h-10" onClick={onCancel}>Cancel</Button>
            <Button variant="primary" size="sm" className="!h-10" onClick={() => onSave({ domain, summary, rationale, outcome, tags })}>
              Save
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="text-[15px] font-medium text-fg line-clamp-2" title={decision.summary}>{decision.summary}</div>
          {decision.rationale && <div className="text-sm text-fg-muted line-clamp-2" title={`Rationale: ${decision.rationale}`}><span className="text-fg-muted">Rationale: </span>{decision.rationale}</div>}
          {decision.outcome && <div className="text-sm text-fg-muted line-clamp-2" title={`Outcome: ${decision.outcome}`}><span className="text-fg-muted">Outcome: </span>{decision.outcome}</div>}
          {decision.tags && <div className="text-sm text-fg-subtle truncate" title={`Tags: ${decision.tags}`}>Tags: {decision.tags}</div>}
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

  if (loading) return <div className="text-fg-muted text-[15px] py-4">Loading…</div>;
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
    <div className="py-3.5">
      <div className="flex items-center justify-between gap-3 mb-1">
        <div className="flex items-center gap-2 text-sm text-fg-muted">
          <span className="px-2 py-0.5 rounded-lg bg-surface-overlay text-fg font-medium capitalize">{editing ? status : initiative.status}</span>
          <span>updated {formatDate(initiative.updated_at)}</span>
        </div>
        {!editing && (
          <OverflowMenu
            size="sm"
            label="Memory actions"
            items={[
              { label: "Edit", onSelect: onEdit },
              { label: "Delete", danger: true, onSelect: onDelete },
            ]}
          />
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <textarea
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Summary"
            rows={2}
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <div className="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" className="!h-10" onClick={onCancel}>Cancel</Button>
            <Button variant="primary" size="sm" className="!h-10" onClick={() => onSave({ title, status, summary })}>
              Save
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="text-[15px] text-fg font-semibold line-clamp-2" title={initiative.title}>{initiative.title}</div>
          {initiative.summary && <div className="text-sm text-fg-muted line-clamp-2" title={initiative.summary}>{initiative.summary}</div>}
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

  if (loading) return <div className="text-fg-muted text-[15px] py-4">Loading…</div>;
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
    <div className="py-3.5">
      <div className="flex items-center justify-between gap-3 mb-1">
        <div className="flex items-center gap-2 text-sm text-fg-muted">
          <span className="px-2 py-0.5 rounded-lg bg-surface-overlay text-fg font-medium capitalize">{editing ? domain : advice.domain}</span>
          <span>{formatDate(advice.timestamp)}</span>
        </div>
        {!editing && (
          <OverflowMenu
            size="sm"
            label="Memory actions"
            items={[
              { label: "Edit", onSelect: onEdit },
              { label: "Delete", danger: true, onSelect: onDelete },
            ]}
          />
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <select
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <input
            type="text"
            value={querySummary}
            onChange={(e) => setQuerySummary(e.target.value)}
            placeholder="What the user asked"
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <textarea
            value={adviceSummary}
            onChange={(e) => setAdviceSummary(e.target.value)}
            placeholder="Advice given"
            rows={3}
            className="w-full bg-surface border border-line rounded-xl px-3 py-2.5 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
          <div className="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" className="!h-10" onClick={onCancel}>Cancel</Button>
            <Button variant="primary" size="sm" className="!h-10" onClick={() => onSave({ domain, query_summary: querySummary, advice_summary: adviceSummary })}>
              Save
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="text-sm text-fg-muted line-clamp-2" title={`Q: ${advice.query_summary}`}>Q: {advice.query_summary}</div>
          <div className="text-[15px] text-fg line-clamp-2" title={advice.advice_summary}>{advice.advice_summary}</div>
        </div>
      )}
    </div>
  );
}
