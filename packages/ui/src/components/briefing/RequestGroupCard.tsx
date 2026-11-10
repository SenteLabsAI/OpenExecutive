"use client";

// One card for the approvals a single request held together (the same chat
// turn asking for the same kind of step, e.g. saving four files): a box per
// item, all ticked, then "Approve N" for the ticked ones and "Skip all".
// Every item is decision-backed, so Approve and Skip go through /decisions,
// as each card would on its own (a Skip of one already answered elsewhere
// counts as done). Held chat steps keep their rule: Approve
// stays off until what each one would do has loaded.

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  approveDecision,
  dismissActionCard,
  getHeldAction,
  type HeldActionPayload,
  type ProposalItem,
} from "@/lib/api";
import { approveLabel, newestAnchor } from "@/lib/needsYou";

import { isHeldAction } from "./HeldActionCard";
import CardTopRow from "./CardTopRow";

// The words every card of a held request starts with, left off each row.
const LEAD_INS = ["Waiting for your OK: "];

function rowText(headline: string): string {
  for (const lead of LEAD_INS) {
    if (headline.startsWith(lead)) return headline.slice(lead.length);
  }
  return headline;
}

export default function RequestGroupCard({
  proposals,
  onResolved,
  emphasized = false,
}: {
  proposals: ProposalItem[];
  // The items that were approved or skipped, so the briefing can drop them.
  onResolved: (done: ProposalItem[]) => void;
  emphasized?: boolean;
}) {
  // The items the reader unticked; everything else is ticked, so an item
  // that joins the group after a refresh starts ticked like the rest.
  const [unticked, setUnticked] = useState<Set<number>>(() => new Set());
  const [held, setHeld] = useState<Record<number, HeldActionPayload>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  const needsLoad = proposals.filter((p) => isHeldAction(p) && p.decision_instance_id != null);
  const loadKey = needsLoad.map((p) => p.decision_instance_id).join(",");
  useEffect(() => {
    if (!loadKey) return;
    const controller = new AbortController();
    for (const id of loadKey.split(",").map(Number)) {
      getHeldAction(id, controller.signal)
        .then((payload) => setHeld((prev) => ({ ...prev, [id]: payload })))
        .catch((err) => {
          if ((err as Error)?.name !== "AbortError") setError("Couldn't load what one of these would do. Untick it to approve the rest.");
        });
    }
    return () => controller.abort();
  }, [loadKey]);

  const chosen = proposals.filter((p) => !unticked.has(p.alert_id));
  // Only the ticked items must have loaded: one that failed to load can be
  // unticked and the rest approved.
  const loaded = chosen.every(
    (p) => !isHeldAction(p) || p.decision_instance_id == null || held[p.decision_instance_id] != null,
  );

  async function run(items: ProposalItem[], act: (id: number) => Promise<unknown>) {
    setBusy(true);
    setError(null);
    const done: ProposalItem[] = [];
    let failed = 0;
    for (const p of items) {
      if (p.decision_instance_id == null) continue;
      try {
        await act(p.decision_instance_id);
        done.push(p);
      } catch {
        failed += 1;
      }
    }
    setBusy(false);
    if (failed > 0) setError(`${failed} didn't go through. Try again.`);
    if (done.length > 0) onResolved(done);
  }

  function toggle(id: number) {
    setUnticked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <CardTopRow
        need="Needs your OK"
        mode="take_the_lead"
        at={newestAnchor(proposals)}
        waitingOn={proposals.find((p) => p.waiting_on)?.waiting_on}
      />
      <div className="min-w-0 text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
        {proposals.length} steps from one request
      </div>
      <ul className="mt-3 flex flex-col gap-1 rounded-xl border border-line bg-surface p-2">
        {proposals.map((p) => {
          const details = p.decision_instance_id != null ? held[p.decision_instance_id]?.details ?? [] : [];
          return (
            <li key={p.alert_id} id={`alert-${p.alert_id}`} className="rounded-lg px-1 py-1">
              <div className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-1 h-4 w-4 shrink-0 accent-[rgb(var(--accent-strong))]"
                  checked={!unticked.has(p.alert_id)}
                  onChange={() => toggle(p.alert_id)}
                  disabled={busy}
                  aria-label={rowText(p.headline)}
                />
                <span className="min-w-0 flex-1 break-words text-[15px] leading-snug text-fg">
                  {rowText(p.headline)}
                </span>
                {details.length > 0 && (
                  <button
                    type="button"
                    className="flex-shrink-0 text-sm font-medium text-accent hover:underline"
                    onClick={() => setOpen(open === p.alert_id ? null : p.alert_id)}
                    aria-expanded={open === p.alert_id}
                  >
                    {open === p.alert_id ? "Hide" : "Details"}
                  </button>
                )}
              </div>
              {open === p.alert_id && details.length > 0 && (
                <dl className="ml-6 mt-2 flex flex-col gap-2">
                  {details.map((d) => (
                    <div key={d.field} className="min-w-0">
                      <dt className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{d.field}</dt>
                      <dd className="whitespace-pre-wrap break-words text-[15px] leading-snug text-fg">{d.value}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </li>
          );
        })}
      </ul>
      <div className="mt-3 grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
        <Button
          variant="primary"
          disabled={busy || chosen.length === 0 || !loaded}
          onClick={() => void run(chosen, approveDecision)}
        >
          {approveLabel(chosen.length)}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={() => void run(proposals, dismissActionCard)}>
          Skip all
        </Button>
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </div>
  );
}
