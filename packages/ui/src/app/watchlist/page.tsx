"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  approveWatchSuggestion,
  createWatchlistItem,
  declineWatchSuggestion,
  deleteWatchlistItem,
  listDepartments,
  listWatchlist,
  patchWatchlistItem,
  type WatchDeclineReason,
  type WatchlistCadence,
  type WatchlistItem,
  type WatchlistSeverity,
  type WatchlistSignalType,
} from "@/lib/api";

// A research suggestion: the Executive wanted to watch this but was not sure
// enough to add it on its own. Sits in dry_run (polls, never alerts) until
// approved or declined here.
function isSuggestion(item: WatchlistItem): boolean {
  return item.origin === "research_proposed" && item.mode === "dry_run";
}

const DECLINE_REASONS: { value: WatchDeclineReason; label: string; hint: string }[] = [
  { value: "not_relevant", label: "Not relevant", hint: "Never suggest this company/topic again" },
  { value: "too_noisy", label: "Too noisy", hint: "Keep it, but only high-severity signals" },
  { value: "wrong_source", label: "Wrong source", hint: "Right topic, drop only this feed/page" },
];

// Rationale + provenance stamp the research policy left on the row.
function policyStamp(item: WatchlistItem): { entity?: string; score?: number; source_url?: string } {
  const raw = (item.config_json as Record<string, unknown> | undefined)?._policy;
  return raw && typeof raw === "object" ? (raw as { entity?: string; score?: number; source_url?: string }) : {};
}

const CADENCES: WatchlistCadence[] = ["real_time", "15min", "hourly", "daily", "weekly"];
const SEVERITIES: WatchlistSeverity[] = ["low", "medium", "high", "urgent"];
const SIGNAL_TYPES: { value: WatchlistSignalType; label: string; hint: string }[] = [
  { value: "stock", label: "Stock", hint: "Yahoo Finance ticker — e.g. AAPL" },
  { value: "rss", label: "RSS / Atom", hint: "Feed URL — e.g. competitor changelog" },
  { value: "vendor_status", label: "Vendor status", hint: "Statuspage atom/RSS feed URL" },
  { value: "edgar", label: "SEC EDGAR", hint: "Ticker or CIK — e.g. AAPL or 320193" },
  {
    value: "page_watch",
    label: "Page change",
    hint: "Public page URL to watch for content changes — e.g. a pricing or careers page",
  },
  {
    value: "query",
    label: "Web search (billed)",
    hint: "Standing search query — e.g. Acme Corp layoffs OR restructuring OR funding",
  },
];

// query runs an LLM web search every poll, so it bills per tick — unlike the
// keyless feed/EDGAR/page adapters. Surfaced as a warning in the add modal.
const BILLED_SIGNAL_TYPES: ReadonlySet<string> = new Set(["query"]);

// Pretty group-header label per signal type, derived from the add-modal's
// SIGNAL_TYPES so the two never drift. Unknown types fall back to the raw value.
const SIGNAL_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  SIGNAL_TYPES.map((s) => [s.value, s.label]),
);

// Group display order: known types in SIGNAL_TYPES order; unknown types sort last.
const SIGNAL_TYPE_ORDER: string[] = SIGNAL_TYPES.map((s) => s.value);

// Turn a kebab-case slug into a readable title: "stock-aapl" → "stock aapl".
// The full slug stays the identity (used for routing + shown on hover).
function humanizeSlug(slug: string): string {
  return slug.replace(/-+/g, " ").trim();
}

// Relative time. Kept inline so the watchlist page is self-contained;
// Briefing.tsx has its own copy with the same formula.
function formatRelTime(iso: string | null): string {
  if (!iso) return "never";
  try {
    const diff = new Date(iso).getTime() - Date.now();
    const abs = Math.abs(diff);
    if (abs < 60_000) return "now";
    if (abs < 3_600_000) return `${Math.round(abs / 60_000)}m`;
    if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)}h`;
    return `${Math.round(abs / 86_400_000)}d`;
  } catch {
    return "—";
  }
}

function ModePill({ mode }: { mode: string }) {
  const isDry = mode === "dry_run";
  const cls = isDry
    ? "bg-zinc-500/20 text-zinc-300 border-zinc-500/30"
    : "bg-indigo-500/20 text-indigo-300 border-indigo-500/30";
  return (
    <span className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${cls}`}>
      {mode}
    </span>
  );
}

function DeclineMenu({
  slug,
  busy,
  onPick,
  label,
}: {
  slug: string;
  busy: boolean;
  onPick: (slug: string, reason: WatchDeclineReason) => void;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        disabled={busy}
        onClick={() => setOpen((o) => !o)}
        className="px-3 py-1.5 text-xs rounded-lg border border-line hover:bg-surface-overlay disabled:opacity-50"
      >
        {label}
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-56 rounded-lg border border-line bg-surface-elevated shadow-lg p-1">
          {DECLINE_REASONS.map((r) => (
            <button
              key={r.value}
              type="button"
              onClick={() => {
                setOpen(false);
                onPick(slug, r.value);
              }}
              className="w-full text-left px-2 py-1.5 rounded hover:bg-surface-overlay"
            >
              <div className="text-xs text-fg">{r.label}</div>
              <div className="text-[10px] text-fg-subtle">{r.hint}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function SuggestionCard({
  item,
  busy,
  onApprove,
  onDecline,
  departmentTitle,
}: {
  item: WatchlistItem;
  busy: boolean;
  onApprove: (slug: string) => void;
  onDecline: (slug: string, reason: WatchDeclineReason) => void;
  departmentTitle?: string;
}) {
  const stamp = policyStamp(item);
  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-fg truncate" title={item.slug}>
            {humanizeSlug(item.slug)}
          </div>
          <div className="text-xs text-fg-muted truncate" title={item.target}>
            {item.signal_type} · {item.target}
          </div>
        </div>
        <span className="flex-shrink-0 inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium bg-amber-500/20 text-amber-200 border-amber-500/30">
          suggested
        </span>
      </div>
      {item.notes && <p className="text-xs text-fg mt-2">{item.notes}</p>}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-muted mt-2">
        {stamp.entity && <span>about: {stamp.entity}</span>}
        {item.route_to_department && (
          <span title="This department's head was asked to review it">
            for: {departmentTitle ?? item.route_to_department}
          </span>
        )}
        <span>suggested {formatRelTime(item.created_at)} ago</span>
        <span>seen in shadow: {item.fired_count} signal{item.fired_count === 1 ? "" : "s"}</span>
        {stamp.source_url && (
          <a href={stamp.source_url} target="_blank" rel="noreferrer" className="text-indigo-300 hover:text-indigo-200">
            source ↗
          </a>
        )}
      </div>
      <div className="flex items-center justify-end gap-2 mt-3 pt-2 border-t border-line">
        <DeclineMenu slug={item.slug} busy={busy} onPick={onDecline} label="Decline…" />
        <button
          type="button"
          disabled={busy}
          onClick={() => onApprove(item.slug)}
          className="px-3 py-1.5 text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium disabled:opacity-50"
        >
          {busy ? "…" : "Approve"}
        </button>
      </div>
    </div>
  );
}

function WatchCard({
  item,
  onToggle,
  toggleBusy,
  onStopWatching,
  departmentTitle,
}: {
  item: WatchlistItem;
  onToggle: (slug: string, enabled: boolean) => void;
  toggleBusy: boolean;
  onStopWatching?: (slug: string, reason: WatchDeclineReason) => void;
  departmentTitle?: string;
}) {
  const isResearch = item.origin === "research";
  const stamp = policyStamp(item);
  return (
    <div className="rounded-xl border border-line bg-surface-elevated hover:bg-surface-overlay transition-colors p-4 group">
      <div className="flex items-start justify-between gap-2 mb-2">
        <Link href={`/watchlist/${encodeURIComponent(item.slug)}`} className="flex-1 min-w-0">
          <div
            className="text-sm font-semibold text-fg group-hover:text-indigo-300 transition-colors truncate"
            title={item.slug}
          >
            {humanizeSlug(item.slug)}
          </div>
          <div className="text-xs text-fg-muted mt-0.5 truncate" title={item.target}>
            {item.target}
          </div>
        </Link>
        <div className="flex-shrink-0 flex items-center gap-2">
          <ModePill mode={item.mode} />
        </div>
      </div>
      {isResearch && (
        <p className="text-[11px] text-fg-muted mb-2">
          <span className="text-indigo-300">Added by the Executive</span>
          {stamp.entity ? ` · about ${stamp.entity}` : ""}
          {item.route_to_department ? ` · for ${departmentTitle ?? item.route_to_department}` : ""}
          {item.notes ? ` · ${item.notes}` : ""}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-muted mb-2">
        <span>cadence: {item.cadence}</span>
        <span>severity: {item.severity_floor}→{item.severity_ceiling}</span>
        <span>fired: {item.fired_count}</span>
        {item.dismiss_count > 0 && <span>dismissed: {item.dismiss_count}</span>}
        <span>last: {formatRelTime(item.last_fired_at)}</span>
      </div>
      <div className="flex items-center justify-between gap-2 pt-2 border-t border-line">
        <label className="flex items-center gap-1.5 text-xs text-fg-muted cursor-pointer">
          <input
            type="checkbox"
            checked={item.enabled}
            disabled={toggleBusy}
            onChange={(e) => onToggle(item.slug, e.target.checked)}
            className="accent-indigo-500 disabled:opacity-50"
          />
          enabled
        </label>
        <div className="flex items-center gap-2">
          {isResearch && onStopWatching && (
            <DeclineMenu slug={item.slug} busy={toggleBusy} onPick={onStopWatching} label="Stop watching…" />
          )}
          <Link
            href={`/watchlist/${encodeURIComponent(item.slug)}`}
            className="text-xs text-indigo-300 hover:text-indigo-200"
          >
            inspect →
          </Link>
        </div>
      </div>
    </div>
  );
}

interface AddModalProps {
  onCreated: (w: WatchlistItem) => void;
  onClose: () => void;
}

function AddWatchModal({ onCreated, onClose }: AddModalProps) {
  const [slug, setSlug] = useState("");
  const [signalType, setSignalType] = useState<WatchlistSignalType>("stock");
  const [target, setTarget] = useState("");
  const [trigger, setTrigger] = useState("");
  const [cadence, setCadence] = useState<WatchlistCadence>("15min");
  const [severityFloor, setSeverityFloor] = useState<WatchlistSeverity>("low");
  const [severityCeiling, setSeverityCeiling] = useState<WatchlistSeverity>("urgent");
  const [mode, setMode] = useState<"active" | "dry_run">("active");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const slugRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    slugRef.current?.focus();
  }, []);

  const targetHint = SIGNAL_TYPES.find((s) => s.value === signalType)?.hint ?? "";

  async function submit() {
    setSaving(true);
    setErr(null);
    let parsedTrigger: Record<string, unknown> = {};
    if (trigger.trim()) {
      try {
        parsedTrigger = JSON.parse(trigger);
      } catch {
        setErr("Trigger must be valid JSON, e.g. {\"abs_change_pct_gte\": 5}");
        setSaving(false);
        return;
      }
    }
    try {
      const created = await createWatchlistItem({
        slug: slug.trim(),
        signal_type: signalType,
        target: target.trim(),
        trigger: parsedTrigger,
        cadence,
        severity_floor: severityFloor,
        severity_ceiling: severityCeiling,
        mode,
        notes: notes.trim(),
      });
      onCreated(created);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Create failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={saving ? undefined : onClose}
    >
      <div
        className="bg-surface-elevated border border-line rounded-xl w-full max-w-lg max-h-[90vh] overflow-y-auto p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-fg mb-4">Add monitor</h2>
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-fg-muted mb-1">Slug (kebab-case)</label>
            <input
              ref={slugRef}
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="stock-aapl"
              className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
            />
          </div>
          <div>
            <label className="block text-xs text-fg-muted mb-1">Signal type</label>
            <select
              value={signalType}
              onChange={(e) => setSignalType(e.target.value as WatchlistSignalType)}
              className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
            >
              {SIGNAL_TYPES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-fg-muted mb-1">Target</label>
            <input
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder={targetHint}
              className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
            />
            <p className="text-[10px] text-fg-subtle mt-1">{targetHint}</p>
            {BILLED_SIGNAL_TYPES.has(signalType) && (
              <p className="text-[10px] text-amber-300/90 mt-1">
                ⚠ Billed: runs an LLM web search on every poll. Prefer a slower
                cadence (daily / weekly) and a tight query.
              </p>
            )}
          </div>
          <div>
            <label className="block text-xs text-fg-muted mb-1">Trigger (JSON, optional)</label>
            <textarea
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              placeholder={
                signalType === "stock"
                  ? '{"abs_change_pct_gte": 5}'
                  : signalType === "edgar"
                    ? '{"forms": ["8-K", "10-K"]}'
                    : signalType === "rss" ||
                        signalType === "query" ||
                        signalType === "page_watch"
                      ? '{"keywords": ["layoffs", "downtime"]}'
                      : "{}"
              }
              rows={3}
              className="w-full px-3 py-2 text-sm font-mono rounded-lg bg-surface-input border border-line"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-fg-muted mb-1">Cadence</label>
              <select
                value={cadence}
                onChange={(e) => setCadence(e.target.value as WatchlistCadence)}
                className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
              >
                {CADENCES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-fg-muted mb-1">Mode</label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as "active" | "dry_run")}
                className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
              >
                <option value="active">active</option>
                <option value="dry_run">dry_run</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-fg-muted mb-1">Severity floor</label>
              <select
                value={severityFloor}
                onChange={(e) => setSeverityFloor(e.target.value as WatchlistSeverity)}
                className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
              >
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-fg-muted mb-1">Severity ceiling</label>
              <select
                value={severityCeiling}
                onChange={(e) => setSeverityCeiling(e.target.value as WatchlistSeverity)}
                className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
              >
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs text-fg-muted mb-1">Notes</label>
            <input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              maxLength={500}
              className="w-full px-3 py-2 text-sm rounded-lg bg-surface-input border border-line"
            />
          </div>
        </div>

        {err && (
          <div className="mt-3 p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
            {err}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <button
            type="button"
            disabled={saving}
            onClick={onClose}
            className="px-4 py-2 text-sm rounded-lg border border-line hover:bg-surface-overlay disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={saving || !slug.trim() || !target.trim()}
            onClick={submit}
            className="px-4 py-2 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium disabled:opacity-50"
          >
            {saving ? "Adding…" : "Add monitor"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function WatchlistPage() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [busySlugs, setBusySlugs] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // slug → title for the "for: <department>" label on routed watches.
  const [departmentTitles, setDepartmentTitles] = useState<Record<string, string>>({});

  const toggleCollapsed = (key: string) =>
    setCollapsed((c) => ({ ...c, [key]: !c[key] }));

  // Group monitors by signal type, mirroring the artifacts/runs collapsible
  // groups. Known types render in SIGNAL_TYPES order; any unknown type sorts
  // last (alphabetically) so a new backend adapter never silently vanishes.
  const suggestions = useMemo(() => items.filter(isSuggestion), [items]);

  const groups = useMemo(() => {
    const map = new Map<string, WatchlistItem[]>();
    for (const it of items) {
      if (isSuggestion(it)) continue;
      const bucket = map.get(it.signal_type);
      if (bucket) bucket.push(it);
      else map.set(it.signal_type, [it]);
    }
    const keys = Array.from(map.keys()).sort((a, b) => {
      const ia = SIGNAL_TYPE_ORDER.indexOf(a);
      const ib = SIGNAL_TYPE_ORDER.indexOf(b);
      if (ia !== -1 && ib !== -1) return ia - ib;
      if (ia !== -1) return -1;
      if (ib !== -1) return 1;
      return a.localeCompare(b);
    });
    return keys.map((k) => ({
      key: k,
      label: SIGNAL_TYPE_LABELS[k] ?? k,
      items: map.get(k)!,
    }));
  }, [items]);

  function refresh() {
    setLoading(true);
    listWatchlist()
      .then(setItems)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    refresh();
    listDepartments()
      .then((states) =>
        setDepartmentTitles(Object.fromEntries(states.map((d) => [d.config.slug, d.config.title]))),
      )
      .catch(() => {});
  }, []);

  function markBusy(slug: string, busy: boolean) {
    setBusySlugs((prev) => {
      const next = new Set(prev);
      if (busy) next.add(slug);
      else next.delete(slug);
      return next;
    });
  }

  async function approve(slug: string) {
    markBusy(slug, true);
    try {
      const updated = await approveWatchSuggestion(slug);
      setItems((prev) => prev.map((it) => (it.slug === slug ? updated : it)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approve failed");
    } finally {
      markBusy(slug, false);
    }
  }

  async function decline(slug: string, reason: WatchDeclineReason) {
    markBusy(slug, true);
    try {
      const res = await declineWatchSuggestion(slug, reason);
      if (res.result === "removed") {
        setItems((prev) => prev.filter((it) => it.slug !== slug));
      } else {
        // too_noisy: the row went live with a high floor — reload it.
        refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decline failed");
    } finally {
      markBusy(slug, false);
    }
  }

  async function stopWatching(slug: string, reason: WatchDeclineReason) {
    markBusy(slug, true);
    try {
      if (reason === "too_noisy") {
        // Same remedy the decline route applies: keep the source, only
        // high-severity signals surface.
        const updated = await patchWatchlistItem(slug, { severity_floor: "high" });
        setItems((prev) => prev.map((it) => (it.slug === slug ? updated : it)));
      } else {
        await deleteWatchlistItem(slug, reason);
        setItems((prev) => prev.filter((it) => it.slug !== slug));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    } finally {
      markBusy(slug, false);
    }
  }

  async function toggle(slug: string, enabled: boolean) {
    // Optimistic. The disabled prop on the input prevents a second
    // click landing while the PATCH is in flight, so the revert path
    // can safely flip back to the prior value.
    setBusySlugs((prev) => new Set(prev).add(slug));
    setItems((prev) =>
      prev.map((it) => (it.slug === slug ? { ...it, enabled } : it)),
    );
    try {
      const updated = await patchWatchlistItem(slug, { enabled });
      setItems((prev) => prev.map((it) => (it.slug === slug ? updated : it)));
    } catch (e) {
      setItems((prev) =>
        prev.map((it) => (it.slug === slug ? { ...it, enabled: !enabled } : it)),
      );
      setError(e instanceof Error ? e.message : "Toggle failed");
    } finally {
      setBusySlugs((prev) => {
        const next = new Set(prev);
        next.delete(slug);
        return next;
      });
    }
  }

  return (
    <div className="flex flex-col h-full bg-surface">
      {showAdd && (
        <AddWatchModal
          onCreated={(w) => {
            setItems((prev) => [...prev, w]);
            setShowAdd(false);
          }}
          onClose={() => setShowAdd(false)}
        />
      )}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6">
          <div className="flex flex-col sm:flex-row sm:items-baseline justify-between gap-3 mb-6">
            <div>
              <h1 className="text-xl font-semibold text-fg">Watch list</h1>
              <p className="text-sm text-fg-muted mt-0.5">
                External conditions the Executive is monitoring. Signals that survive
                severity + triage become proposals in your briefing.
              </p>
            </div>
            <button
              onClick={() => setShowAdd(true)}
              className="self-start flex-shrink-0 px-4 py-2 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium"
            >
              + Add monitor
            </button>
          </div>

          {loading && <p className="text-fg-muted text-sm">Loading…</p>}
          {error && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm mb-4">
              {error}
            </div>
          )}
          {!loading && !error && items.length === 0 && (
            <div className="rounded-xl border border-line bg-surface-elevated p-8 text-center">
              <p className="text-fg-muted text-sm mb-3">Nothing being watched yet.</p>
              <p className="text-xs text-fg-subtle mb-4">
                Ask the Executive in chat:{" "}
                <span className="italic">
                  Watch Apple stock, alert me on 5% moves.
                </span>
              </p>
              <button
                onClick={() => setShowAdd(true)}
                className="px-4 py-2 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white"
              >
                Add a monitor →
              </button>
            </div>
          )}

          {suggestions.length > 0 && (
            <div className="mb-8">
              <div className="flex items-baseline gap-2 mb-1">
                <span className="text-sm font-semibold text-fg">Suggested by the Executive</span>
                <span className="text-xs text-fg-muted">
                  {suggestions.length} waiting for you
                </span>
              </div>
              <p className="text-xs text-fg-subtle mb-3">
                Sources the research council thought worth monitoring but couldn&apos;t tie
                firmly enough to your company data to add on its own. They poll in
                shadow mode and never alert until you approve. Declines are remembered.
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {suggestions.map((item) => (
                  <SuggestionCard
                    key={item.id}
                    item={item}
                    busy={busySlugs.has(item.slug)}
                    onApprove={approve}
                    onDecline={decline}
                    departmentTitle={departmentTitles[item.route_to_department]}
                  />
                ))}
              </div>
            </div>
          )}

          <div className="space-y-6">
            {groups.map((group) => {
              const isCollapsed = !!collapsed[group.key];
              return (
                <div key={group.key}>
                  <button
                    type="button"
                    onClick={() => toggleCollapsed(group.key)}
                    className="w-full flex items-center gap-2 mb-3 text-left"
                  >
                    <span
                      className={`text-fg-muted text-xs transition-transform ${
                        isCollapsed ? "" : "rotate-90"
                      }`}
                    >
                      ▶
                    </span>
                    <span className="text-sm font-semibold text-fg">
                      {group.label}
                    </span>
                    <span className="text-xs text-fg-muted">
                      {group.items.length}{" "}
                      {group.items.length === 1 ? "monitor" : "monitors"}
                    </span>
                  </button>
                  {!isCollapsed && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      {group.items.map((item) => (
                        <WatchCard
                          key={item.id}
                          item={item}
                          onToggle={toggle}
                          toggleBusy={busySlugs.has(item.slug)}
                          onStopWatching={stopWatching}
                          departmentTitle={departmentTitles[item.route_to_department]}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </main>
    </div>
  );
}
