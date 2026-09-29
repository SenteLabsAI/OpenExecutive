"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  listFactApprovalRules,
  listStandingFacts,
  retireStandingFact,
  reviewStandingFact,
  setFactApprovalRule,
  type FactApprovalRule,
  type StandingFact,
} from "@/lib/api";
import { EmptyState, formatDate } from "./shared";

// The "what stuck" view: facts and corrections the principal or a teammate
// told the Executive to keep (the `remember_fact` chat tool) and company-profile
// fields changed from chat (`update_company_profile`). Active facts are read by
// every prompt that produces output — chat, briefs, scheduled runs, the alert
// review — so this is where the owner checks a correction actually held, and
// retires one that no longer does. A teammate's fact is marked with their name;
// one waiting for the owner's approval (it would replace the owner's own fact,
// or the owner asked to approve that teammate's) is listed first, with Approve
// and Decline for the owner. The owner also sets, per teammate, whether their
// facts need approval.

const EMPTY =
  "No corrections yet — when you correct a figure or a fact in chat, the Executive keeps it here and uses it everywhere.";

const KIND_LABEL: Record<StandingFact["kind"], string> = {
  fact: "Fact",
  correction: "Correction",
  profile: "Profile",
};

const KIND_PILL: Record<StandingFact["kind"], string> = {
  fact: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  correction: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  profile: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
};

const HISTORY_STATUSES = new Set<StandingFact["status"]>(["superseded", "retired", "declined"]);

function channelLabel(channel: string): string {
  if (!channel) return "chat";
  if (channel === "web") return "web chat";
  if (channel === "google_chat") return "Google Chat";
  return channel.charAt(0).toUpperCase() + channel.slice(1);
}

export default function CorrectionsTab({ onCount }: { onCount: (n: number) => void }) {
  const [facts, setFacts] = useState<StandingFact[]>([]);
  const [retirable, setRetirable] = useState<Set<number>>(new Set());
  const [canReview, setCanReview] = useState(false);
  const [rules, setRules] = useState<FactApprovalRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listStandingFacts();
      setFacts(page.facts);
      setRetirable(new Set(page.retirable_ids ?? []));
      setCanReview(page.can_review ?? false);
      setFailed(false);
      onCount(page.facts.filter((f) => f.status === "active").length);
      if (page.can_review) {
        // The switch list is extra: failing to load it leaves the facts shown.
        setRules(await listFactApprovalRules().catch(() => []));
      }
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [onCount]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleRetire = useCallback(
    async (fact: StandingFact) => {
      if (!window.confirm(`Stop using this everywhere?\n\n${fact.statement}`)) return;
      try {
        await retireStandingFact(fact.id);
      } catch {
        window.alert("Failed to retire.");
        return;
      }
      void refresh();
    },
    [refresh],
  );

  const handleReview = useCallback(
    async (fact: StandingFact, decision: "approve" | "decline") => {
      try {
        await reviewStandingFact(fact.id, decision);
      } catch {
        window.alert(decision === "approve" ? "Failed to approve." : "Failed to decline.");
        return;
      }
      void refresh();
    },
    [refresh],
  );

  const handleRule = useCallback(async (rule: FactApprovalRule) => {
    try {
      const saved = await setFactApprovalRule(rule.person_id, !rule.needs_approval);
      setRules((prev) => prev.map((r) => (r.person_id === saved.person_id ? saved : r)));
    } catch {
      window.alert("Failed to change who needs approval.");
    }
  }, []);

  if (loading) return <div className="text-fg-muted text-sm">Loading…</div>;
  if (failed) return <EmptyState message="Corrections are unavailable right now." />;

  const proposed = facts.filter((f) => f.status === "proposed");
  const active = facts.filter((f) => f.status === "active");
  const history = facts.filter((f) => HISTORY_STATUSES.has(f.status));
  const byId = new Map(facts.map((f) => [f.id, f]));

  return (
    <div className="space-y-3">
      {facts.length === 0 ? (
        <EmptyState message={EMPTY} />
      ) : (
        <p className="text-xs text-fg-muted">
          These hold in every conversation, brief, scheduled run and alert review.
        </p>
      )}
      {proposed.length > 0 && (
        <div className="rounded border border-amber-500/30 bg-amber-500/5 px-3">
          <div className="pt-2 text-xs font-medium text-amber-300">
            {canReview ? "Waiting for your approval" : "Waiting for the owner's approval"}
          </div>
          <div className="divide-y divide-line">
            {proposed.map((f) => (
              <FactRow
                key={f.id}
                fact={f}
                replaces={f.replaces_fact_id ? byId.get(f.replaces_fact_id) : undefined}
                onApprove={canReview ? () => handleReview(f, "approve") : undefined}
                onDecline={canReview ? () => handleReview(f, "decline") : undefined}
              />
            ))}
          </div>
        </div>
      )}
      {facts.length > 0 &&
        (active.length === 0 ? (
          <div className="text-sm text-fg-muted py-4">Nothing active — every correction has been replaced or retired.</div>
        ) : (
          <div className="divide-y divide-line">
            {active.map((f) => (
              <FactRow
                key={f.id}
                fact={f}
                onRetire={retirable.has(f.id) && f.kind !== "profile" ? () => handleRetire(f) : undefined}
              />
            ))}
          </div>
        ))}
      {history.length > 0 && (
        <div>
          <button
            onClick={() => setShowHistory((v) => !v)}
            className="text-xs text-fg-muted hover:text-fg"
          >
            {showHistory ? "Hide" : "Show"} history ({history.length})
          </button>
          {showHistory && (
            <div className="divide-y divide-line opacity-70">
              {history.map((f) => (
                <FactRow key={f.id} fact={f} replacedBy={f.superseded_by ? byId.get(f.superseded_by) : undefined} />
              ))}
            </div>
          )}
        </div>
      )}
      {canReview && rules.length > 0 && (
        <div className="border-t border-line pt-3">
          <div className="text-xs font-medium text-fg">Teammates&apos; corrections</div>
          <p className="text-xs text-fg-muted mb-2">
            Teammates can correct facts too; theirs are marked with their name and never replace yours.
            Turn on &ldquo;Needs my approval&rdquo; to check a teammate&apos;s corrections before they are used.
          </p>
          <ul className="space-y-1">
            {rules.map((r) => (
              <li key={r.person_id}>
                <label className="flex items-center gap-2 text-sm text-fg cursor-pointer">
                  <input
                    type="checkbox"
                    checked={r.needs_approval}
                    onChange={() => void handleRule(r)}
                  />
                  <span>{r.full_name}</span>
                  <span className="text-xs text-fg-muted">— needs my approval</span>
                </label>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function FactRow({
  fact,
  onRetire,
  onApprove,
  onDecline,
  replacedBy,
  replaces,
}: {
  fact: StandingFact;
  onRetire?: () => void;
  onApprove?: () => void;
  onDecline?: () => void;
  replacedBy?: StandingFact;
  replaces?: StandingFact;
}) {
  return (
    <div className="group py-3 hover:bg-surface-overlay/30 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-1.5">
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted min-w-0">
          <span className={`px-2 py-0.5 rounded border font-medium ${KIND_PILL[fact.kind]}`}>
            {KIND_LABEL[fact.kind]}
          </span>
          <span className="truncate" title={fact.subject}>{fact.subject}</span>
          <span>· {formatDate(fact.created_at)} via {channelLabel(fact.source_channel)}</span>
          {fact.recorded_by_role === "teammate" && (
            <span className="text-fg">· per {fact.recorded_by_name || "a teammate"}</span>
          )}
        </div>
        {(onApprove || onDecline) && (
          <div className="shrink-0 flex gap-3 text-xs">
            {onApprove && (
              <button onClick={onApprove} className="text-emerald-400 hover:text-emerald-300">
                Approve
              </button>
            )}
            {onDecline && (
              <button onClick={onDecline} className="text-red-400 hover:text-red-300">
                Decline
              </button>
            )}
          </div>
        )}
        {onRetire && (
          <button
            onClick={onRetire}
            className="shrink-0 text-xs text-red-400 hover:text-red-300 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
          >
            Retire
          </button>
        )}
        {fact.kind === "profile" && fact.status === "active" && (
          <Link href="/company-profile" className="shrink-0 text-xs text-fg-muted hover:text-fg">
            Company profile
          </Link>
        )}
      </div>
      <div className="text-sm text-fg">{fact.statement}</div>
      {fact.previous_statement && (
        <div className="text-xs text-fg-muted mt-0.5">
          <span className="line-through">{fact.previous_statement}</span>
        </div>
      )}
      {fact.source_quote && (
        <div className="text-xs text-fg-subtle italic mt-1 line-clamp-2" title={fact.source_quote}>
          “{fact.source_quote}”
        </div>
      )}
      {fact.status === "proposed" && replaces && (
        <div className="text-xs text-fg-subtle mt-1">Would replace: {replaces.statement}</div>
      )}
      {fact.status === "superseded" && (
        <div className="text-xs text-fg-subtle mt-1">
          Replaced{replacedBy ? ` by: ${replacedBy.statement}` : ""}
        </div>
      )}
      {(fact.status === "retired" || fact.status === "declined") && (
        <div className="text-xs text-fg-subtle mt-1">
          {fact.status === "declined" ? "Declined" : "Retired"} {fact.retired_at ? formatDate(fact.retired_at) : ""}
          {fact.retired_reason ? ` — ${fact.retired_reason}` : ""}
        </div>
      )}
    </div>
  );
}
