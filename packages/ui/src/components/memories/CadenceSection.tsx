"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  cancelScheduledAction,
  getActivity,
  listScheduledActions,
  type ActivityItem,
  type ScheduledAction,
} from "@/lib/api";
import Icon, { type IconName } from "@/components/Icon";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  LivePulse,
  STATUS_PILL,
  SectionHeading,
  Skeleton,
  type TagTone,
  formatRunAt,
  groupByRhythm,
  metaFor,
  showsRhythm,
} from "./shared";

// The "how it runs" half of the Pulse page. The rhythm taxonomy (KIND_META /
// metaFor / groupByRhythm) now lives in ./shared so the header stat strip and
// these cards agree on how raw scheduled_actions `kind`s map to human groups.
//
// The rhythm card shows the *pending* queue only (the upcoming cadence),
// fetched soonest-first. Follow-ups (ad_hoc one-offs) are pulled out of this
// card into their own `FollowUpsCard`, rendered under Memory on the Pulse page.

// Over-fetch cap. The backend can't filter by `kind`, so we pull a generous
// slice and group client-side; high-frequency system scans are capped at render
// time (see SystemPulse) so they can't bury the user-facing groups.
const FETCH_LIMIT = 500;

// ---------------------------------------------------------------------------
// Section shell
// ---------------------------------------------------------------------------

export default function CadenceSection() {
  // Solo hides the department check-ins and the awaiting-people block.
  const { mode } = useWorkspace();
  const [rows, setRows] = useState<ScheduledAction[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      // Pending only — the upcoming cadence, soonest-first.
      const data = await listScheduledActions("pending", FETCH_LIMIT, signal, "asc");
      if (!signal?.aborted) setRows(data);
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      if (!signal?.aborted) setRows([]);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  // groupByRhythm sorts each group soonest-first and deliberately drops ad_hoc
  // (those are the Follow-ups, now their own card under Memory).
  const groups = useMemo(() => groupByRhythm(rows), [rows]);

  // Whether any rhythm group has rows to show. We gate the empty-state on this
  // rather than `rows.length`, because `rows` still contains the ad_hoc
  // follow-ups (dropped by groupByRhythm) — without this, a queue of only
  // follow-ups would render an empty padded card instead of the empty message.
  const hasRhythm =
    groups.daily.length > 0 ||
    (showsRhythm("departments", mode) && groups.departments.length > 0) ||
    (showsRhythm("awaiting", mode) && groups.awaiting.length > 0) ||
    groups.system.length > 0;

  return (
    <div className="space-y-8">
      <RecentActivity />

      <div className="rounded-xl border border-line bg-surface-elevated p-4">
        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : !hasRhythm ? (
          <div className="rounded-lg border border-line bg-surface-elevated/40 px-4 py-6 text-sm text-fg-muted">
            No recurring cadence is scheduled yet. The heartbeat starts once the
            scheduler is running and a company profile is set.
          </div>
        ) : (
          <div className="max-h-[32rem] overflow-y-auto pr-1 space-y-6">
            <RhythmBlock
              title="Daily rhythm"
              subtitle="Your daily briefing cycle — the morning brief and end-of-day digest are sent to you by email or chat (Settings → Setup status shows where); the reflection sets up the morning brief."
              icon="clipboard"
              tag="Once a day · for you"
              tagTone="info"
              actions={groups.daily}
            />
            {showsRhythm("departments", mode) && (
              <RhythmBlock
                title="Department check-ins"
                subtitle="Each team's cadence — the next scheduled check-in per department."
                icon="grid"
                actions={groups.departments}
                showDepartment
              />
            )}

            {showsRhythm("awaiting", mode) && (
              <RhythmBlock
                title="Awaiting people"
                subtitle="Paused workflows and nudges waiting on a reply."
                icon="bell"
                actions={groups.awaiting}
              />
            )}

            <SystemPulse actions={groups.system} />
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recent activity — the literal heartbeat. A live timeline of the most recent
// self-initiated actions (reuses GET /today/activity). Independent of the
// status filter — it's always the live "what just happened" feed.
// ---------------------------------------------------------------------------

// One-line verb + subject for an activity item — the `kind` decides the verb;
// `subject` names who/what it was directed at. Ported from the Briefing rail
// (now removed) so the Pulse shows the same rich rows. The full summary is
// always rendered as the body, so no information is lost.
function activityLine(item: ActivityItem): { verb: string; subject: string } {
  switch (item.kind) {
    case "dm_sent":
      return { verb: "DM'd", subject: item.target ?? "a colleague" };
    case "email_sent":
      return { verb: "emailed", subject: item.target ?? "a colleague" };
    case "nudge_sent":
      return { verb: "nudged", subject: item.target ?? "a stalled item" };
    case "cadence_sent":
      return { verb: "ran cadence for", subject: item.department ?? "a department" };
    case "workflow_resumed":
      return { verb: "resumed workflow with", subject: item.target ?? "someone" };
    case "proposal_routed":
      // propose_only path — backend marked the action done without dispatching,
      // so describe the intent ("proposed to X") rather than implying a send.
      return { verb: "proposed to", subject: item.target ?? "an approver" };
    case "decision_logged":
      return { verb: "logged decision:", subject: item.summary };
    case "advice_given":
      return { verb: "advised on", subject: item.summary };
    case "workflow_done":
      return { verb: "completed", subject: item.summary };
    case "initiative_started":
      return { verb: "kicked off initiative:", subject: item.summary };
    case "decision_resolved":
      return { verb: "resolved decision:", subject: item.summary };
    case "alert_raised":
      return { verb: "raised alert:", subject: item.summary };
    default:
      return { verb: "acted on", subject: item.summary };
  }
}

// Kinds whose `subject` IS the full summary: the body row renders the text, so
// the inline subject span is suppressed to avoid repeating it (only the verb
// shows inline). Kept beside activityLine so adding a kind is a one-place edit.
const SUMMARY_KINDS = new Set([
  "decision_logged",
  "advice_given",
  "action",
  "workflow_done",
  "initiative_started",
  "decision_resolved",
  "alert_raised",
]);

// Live activity refreshes on this cadence so new events stream into the feed
// without a reload (mirrors the departments page poll). Kept modest — the feed
// is read-only and the payload is small.
const ACTIVITY_POLL_INTERVAL_MS = 20_000;

function RecentActivity() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    // The Pulse is the single home for recent activity (removed from the
    // Briefing), so pull a generous slice (the list scrolls internally) and
    // then refresh on an interval so the feed stays live.
    const load = () => {
      getActivity(100)
        .then((res) => {
          if (!cancelled) setItems(res.items);
        })
        .catch(() => {
          // Swallow transient errors — keep the last good list and let the
          // next tick retry rather than blanking the feed.
        })
        .finally(() => {
          // Only the first load drives the skeleton; interval refreshes update
          // silently so the feed doesn't flash on every tick.
          if (!cancelled) setLoading(false);
        });
    };

    load();
    const interval = window.setInterval(load, ACTIVITY_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-xl border border-line bg-surface-elevated p-4 space-y-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
      </div>
    );
  }
  if (items.length === 0) return null;

  return (
    <div className="rounded-xl border border-line bg-surface-elevated p-4">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          <Icon name="activity" size="w-4 h-4" className="text-emerald-400" />
          <h3 className="text-sm font-semibold text-fg">Recent activity</h3>
        </div>
        <LivePulse />
      </div>
      {/* Fixed-height scroll region so the feed scrolls internally instead of
          growing the page. The timeline line lives on the inner <ol> (sized to
          the full list) so it scrolls with the rows rather than clipping. */}
      <div className="max-h-[32rem] overflow-y-auto pr-1">
        <ol className="relative space-y-3 before:absolute before:left-[3px] before:top-1.5 before:bottom-1.5 before:w-px before:bg-line">
          {items.map((it, i) => {
            const { relative } = formatRunAt(it.at);
            const { verb, subject } = activityLine(it);
            // For summary-style kinds the subject IS the summary, so don't
            // repeat it inline — the body row below carries the text.
            const subjectIsSummary = SUMMARY_KINDS.has(it.kind);
            return (
              <li key={`${it.at}-${it.kind}-${i}`} className="relative pl-5">
                <span
                  className="absolute left-0 top-1.5 h-[7px] w-[7px] rounded-full bg-emerald-400/80 ring-2 ring-surface-elevated"
                  aria-hidden="true"
                />
                <div className="flex items-baseline gap-2 flex-wrap">
                  {relative && (
                    <span className="text-[11px] text-fg-subtle tabular-nums">{relative}</span>
                  )}
                  <span className="text-xs text-fg-muted">{verb}</span>
                  {!subjectIsSummary && (
                    <span className="text-xs text-fg" title={subject}>{subject}</span>
                  )}
                  {it.department && (
                    <span className="ml-auto text-[10px] text-fg-subtle hidden sm:inline">
                      {it.department}
                    </span>
                  )}
                </div>
                {it.summary && (
                  <p className="text-xs text-fg-muted leading-snug break-words line-clamp-2 mt-0.5" title={it.summary}>
                    {it.summary}
                  </p>
                )}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Read-only rhythm cards (recurring briefs, dept check-ins, awaiting items).
// ---------------------------------------------------------------------------

function RhythmBlock({
  title,
  subtitle,
  icon,
  tag,
  tagTone,
  actions,
  showDepartment = false,
}: {
  title: string;
  subtitle: string;
  icon: IconName;
  tag?: string;
  tagTone?: TagTone;
  actions: ScheduledAction[];
  showDepartment?: boolean;
}) {
  if (actions.length === 0) return null;
  return (
    <section>
      <SectionHeading
        title={title}
        count={actions.length}
        icon={icon}
        tag={tag}
        tagTone={tagTone}
        subtitle={subtitle}
      />
      <div className="divide-y divide-line">
        {actions.map((a) => (
          <RhythmCard key={a.id} action={a} showDepartment={showDepartment} />
        ))}
      </div>
    </section>
  );
}

function RhythmCard({
  action,
  showDepartment,
}: {
  action: ScheduledAction;
  showDepartment: boolean;
}) {
  const meta = metaFor(action);
  const { absolute, relative } = formatRunAt(action.run_at);
  const isPending = action.status === "pending";
  return (
    <div className="group py-2.5 hover:bg-surface-overlay/30 transition-colors flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm text-fg font-medium" title={meta.label}>{meta.label}</span>
          {showDepartment && action.department && (
            <span className="px-2 py-0.5 rounded bg-surface-overlay text-fg text-[11px] font-medium capitalize">
              {action.department}
            </span>
          )}
        </div>
        {meta.blurb && (
          <div className="text-xs text-fg-muted mt-0.5 line-clamp-2" title={meta.blurb}>
            {meta.blurb}
          </div>
        )}
        {action.last_error && action.status === "failed" && (
          <div className="text-xs text-red-400 mt-1 font-mono break-words">
            {action.last_error}
          </div>
        )}
      </div>
      <div className="text-right whitespace-nowrap shrink-0">
        {isPending ? (
          <div className="text-xs text-sky-300" title={absolute}>
            {relative ? `next ${relative}` : absolute}
          </div>
        ) : (
          <div className="flex flex-col items-end gap-1">
            <span
              className={`px-2 py-0.5 rounded border text-[10px] font-medium capitalize ${
                STATUS_PILL[action.status] ?? STATUS_PILL.cancelled
              }`}
            >
              {action.status}
            </span>
            <span className="text-xs text-fg-subtle" title={absolute}>
              {relative || absolute}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// System pulse — the literal heartbeat (internal scans). Shown like the other
// rhythm groups (shared SectionHeading) and always expanded. Capped at render
// time: under done/all these fire every few minutes, so an uncapped list would
// bury the user-facing groups above.
// ---------------------------------------------------------------------------

const SYSTEM_PULSE_CAP = 50;

function SystemPulse({ actions }: { actions: ScheduledAction[] }) {
  if (actions.length === 0) return null;
  const shown = actions.slice(0, SYSTEM_PULSE_CAP);
  const hidden = actions.length - shown.length;
  return (
    <section>
      <SectionHeading
        title="System pulse"
        count={actions.length}
        icon="activity"
        tag="Internal · continuous"
        tagTone="muted"
        subtitle="Background scans that run every few minutes to keep the Executive aware of change. Nothing here is sent to you — findings surface later as proposals or nudges."
      />
      <div className="divide-y divide-line">
        {shown.map((a) => (
          <RhythmCard key={a.id} action={a} showDepartment={false} />
        ))}
      </div>
      {hidden > 0 && (
        <div className="text-xs text-fg-subtle mt-2">+{hidden} more not shown</div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Follow-ups — one-off ad_hoc commitments, surfaced as their own card under
// Memory on the Pulse page. Self-contained: fetches the pending queue itself
// (independent of the rhythm card) and owns the per-row cancel. Hidden when
// there are no pending follow-ups, like the rhythm blocks.
// ---------------------------------------------------------------------------
export function FollowUpsCard() {
  const [rows, setRows] = useState<ScheduledAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  // Bumped after a cancel to retrigger the AbortController-guarded fetch effect.
  const [reloadNonce, setReloadNonce] = useState(0);

  // `loading` is only ever true for the first load (initial state). Refetches
  // (after a cancel) deliberately DON'T flip it back to true, so the list stays
  // visible and the cancelled row just drops out when fresh data arrives —
  // rather than flashing a skeleton mid-cancel.
  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await listScheduledActions("pending", FETCH_LIMIT, signal, "asc");
      if (!signal?.aborted) setRows(data);
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      if (!signal?.aborted) setRows([]);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh, reloadNonce]);

  const handleCancel = useCallback(async (id: number) => {
    if (!window.confirm("Cancel this follow-up? It won't fire.")) return;
    setCancellingId(id);
    try {
      await cancelScheduledAction(id);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Failed to cancel.");
      setCancellingId(null);
      return;
    }
    setCancellingId(null);
    setReloadNonce((n) => n + 1);
  }, []);

  // ad_hoc one-offs, soonest-first (groupByRhythm deliberately drops these).
  const followups = useMemo(() => {
    const f = rows.filter((r) => r.kind === "ad_hoc");
    f.sort((a, b) => a.run_at.localeCompare(b.run_at));
    return f;
  }, [rows]);

  if (loading) {
    return (
      <div className="rounded-xl border border-line bg-surface-elevated p-4 space-y-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }
  if (followups.length === 0) return null;

  return (
    <div className="rounded-xl border border-line bg-surface-elevated p-4">
      <SectionHeading
        title="Follow-ups"
        count={followups.length}
        icon="flag"
        subtitle="One-off commitments the Executive scheduled for you."
      />
      <div className="max-h-[32rem] overflow-y-auto pr-1 divide-y divide-line">
        {followups.map((a) => (
          <FollowUpRow
            key={a.id}
            action={a}
            cancelling={cancellingId === a.id}
            onCancel={() => handleCancel(a.id)}
          />
        ))}
      </div>
    </div>
  );
}

function FollowUpRow({
  action,
  cancelling,
  onCancel,
}: {
  action: ScheduledAction;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const { absolute, relative } = formatRunAt(action.run_at);
  const pill = STATUS_PILL[action.status] ?? STATUS_PILL.cancelled;
  return (
    <div className="group py-3 hover:bg-surface-overlay/30 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 text-xs text-fg-muted flex-wrap min-w-0">
          <span className={`px-2 py-0.5 rounded border font-medium capitalize ${pill}`}>
            {action.status}
          </span>
          <span className="px-2 py-0.5 rounded bg-surface-overlay text-fg font-medium">
            {action.channel}
          </span>
          <span className="text-fg-subtle">→</span>
          <span className="text-fg-muted font-mono text-[11px] truncate max-w-[12rem]" title={action.channel_ref}>{action.channel_ref}</span>
          <span title={absolute}>{relative || absolute}</span>
          {action.attempts > 0 && (
            <span className="text-amber-400">{action.attempts} attempt{action.attempts === 1 ? "" : "s"}</span>
          )}
        </div>
        {action.status === "pending" && (
          <button
            onClick={onCancel}
            disabled={cancelling}
            className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity shrink-0"
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        )}
      </div>
      <div className="text-sm text-fg break-words line-clamp-2" title={action.intent_text}>
        {action.intent_text}
      </div>
      {action.last_error && (
        <div className="text-xs text-red-400 mt-2 font-mono break-words line-clamp-2" title={action.last_error}>
          {action.last_error}
        </div>
      )}
      {action.originating_session_id && (
        <div className="text-[11px] text-fg-subtle mt-2 font-mono truncate" title={`session: ${action.originating_session_id}`}>
          session: {action.originating_session_id}
        </div>
      )}
    </div>
  );
}
