"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  listStandingFacts,
  retireStandingFact,
  type StandingFact,
} from "@/lib/api";
import { EmptyState, formatDate } from "./shared";

// The "what stuck" view: facts and corrections the principal told the
// Executive to keep (the `remember_fact` chat tool) and company-profile fields
// changed from chat (`update_company_profile`). Active facts are read by every
// prompt that produces output — chat, briefs, scheduled runs, the alert
// review — so this is where the owner checks a correction actually held, and
// retires one that no longer does.

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

function channelLabel(channel: string): string {
  if (!channel) return "chat";
  if (channel === "web") return "web chat";
  if (channel === "google_chat") return "Google Chat";
  return channel.charAt(0).toUpperCase() + channel.slice(1);
}

export default function CorrectionsTab({ onCount }: { onCount: (n: number) => void }) {
  const [facts, setFacts] = useState<StandingFact[]>([]);
  const [canRetire, setCanRetire] = useState(false);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listStandingFacts();
      setFacts(page.facts);
      setCanRetire(page.can_retire);
      setFailed(false);
      onCount(page.facts.filter((f) => f.status === "active").length);
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

  if (loading) return <div className="text-fg-muted text-sm">Loading…</div>;
  if (failed) return <EmptyState message="Corrections are unavailable right now." />;
  if (facts.length === 0) return <EmptyState message={EMPTY} />;

  const active = facts.filter((f) => f.status === "active");
  const history = facts.filter((f) => f.status !== "active");
  const byId = new Map(facts.map((f) => [f.id, f]));

  return (
    <div className="space-y-3">
      <p className="text-xs text-fg-muted">
        These hold in every conversation, brief, scheduled run and alert review.
      </p>
      {active.length === 0 ? (
        <div className="text-sm text-fg-muted py-4">Nothing active — every correction has been replaced or retired.</div>
      ) : (
        <div className="divide-y divide-line">
          {active.map((f) => (
            <FactRow
              key={f.id}
              fact={f}
              onRetire={canRetire && f.kind !== "profile" ? () => handleRetire(f) : undefined}
            />
          ))}
        </div>
      )}
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
    </div>
  );
}

function FactRow({
  fact,
  onRetire,
  replacedBy,
}: {
  fact: StandingFact;
  onRetire?: () => void;
  replacedBy?: StandingFact;
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
        </div>
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
      {fact.status === "superseded" && (
        <div className="text-xs text-fg-subtle mt-1">
          Replaced{replacedBy ? ` by: ${replacedBy.statement}` : ""}
        </div>
      )}
      {fact.status === "retired" && (
        <div className="text-xs text-fg-subtle mt-1">
          Retired {fact.retired_at ? formatDate(fact.retired_at) : ""}
          {fact.retired_reason ? ` — ${fact.retired_reason}` : ""}
        </div>
      )}
    </div>
  );
}
