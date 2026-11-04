"use client";

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import { approveDecision, getDecision, rejectDecision, type DecisionInstance } from "@/lib/api";

// The chat: a step the Executive held for your OK (orchestrator/levels.py,
// or a teammate's step their department held), shown under its reply.
// Approve carries out exactly the call shown, on the server; Not now drops
// it. The same card is on Today and in your chat app.

interface HeldPayload {
  summary: string;
  reason: string;
  details: { field: string; value: string }[];
}

function readPayload(instance: DecisionInstance): HeldPayload | null {
  try {
    const raw = JSON.parse(instance.proposed_payload_json || "{}");
    if (typeof raw.summary !== "string") return null;
    const details = Array.isArray(raw.details) ? raw.details : [];
    return {
      summary: raw.summary,
      reason: typeof raw.reason === "string" ? raw.reason : "",
      details: details.map((d: { field?: unknown; value?: unknown }) => ({
        field: String(d.field ?? ""),
        value: String(d.value ?? ""),
      })),
    };
  } catch {
    return null;
  }
}

const DONE = new Set(["approved_unchanged", "approved_with_edit", "executed"]);

export default function ChatHeldActionCard({ id }: { id: number }) {
  const [instance, setInstance] = useState<DecisionInstance | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getDecision(id, controller.signal)
      .then(setInstance)
      .catch(() => { /* not this person's to see, or gone: the chip stays */ });
    return () => controller.abort();
  }, [id]);

  if (!instance || instance.decision_class !== "department_action") return null;
  const payload = readPayload(instance);
  if (!payload) return null;
  const done = DONE.has(instance.status);
  const waiting = instance.status === "proposed";
  if (!done && !waiting) return null;

  const act = async (label: string, call: () => Promise<DecisionInstance>) => {
    setBusy(label);
    setError(null);
    try {
      setInstance(await call());
    } catch (err) {
      setError(err instanceof Error ? err.message : "That didn't go through.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Waiting for your OK</div>
      <div className="mt-1 text-base sm:text-[17px] font-semibold leading-snug text-fg break-words whitespace-pre-line">
        {payload.summary}
      </div>
      {payload.reason && !done && (
        <p className="mt-1.5 text-sm text-fg-muted">I waited because {payload.reason}.</p>
      )}
      {payload.details.length > 0 && !done && (
        <dl className="mt-3 flex flex-col gap-2 rounded-xl border border-line bg-surface p-3">
          {payload.details.map((d) => (
            <div key={d.field} className="min-w-0">
              <dt className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{d.field}</dt>
              <dd className="whitespace-pre-wrap break-words text-[15px] leading-snug text-fg">{d.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {done ? (
        <p className="mt-3 text-sm font-semibold text-emerald-700 dark:text-emerald-300">✓ Done.</p>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            onClick={() => void act("Doing it…", () => approveDecision(instance.id))}
            disabled={busy !== null}
          >
            {busy ?? "Approve"}
          </Button>
          <Button
            variant="ghost"
            className="ml-auto"
            onClick={() => void act("Declining…", () => rejectDecision(instance.id))}
            disabled={busy !== null}
          >
            Not now
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}
