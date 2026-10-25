"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import Button from "@/components/ui/Button";
import {
  approveAppAction,
  getDecision,
  rejectDecision,
  type DecisionInstance,
} from "@/lib/api";

// The chat: something you asked the Executive to do in the app that waits
// for your OK (a delete, a share, turning on something that acts as you),
// shown under its reply. Confirm runs it with your own sign-in, through the
// same route as the button on its screen; Cancel drops it. Only you see it
// (the server answers anyone else 404), and a card left a day goes stale.

interface AppActionPayload {
  action: string;
  id: string;
  card: string;
}

function readPayload(instance: DecisionInstance): AppActionPayload | null {
  try {
    const raw = JSON.parse(instance.proposed_payload_json || "{}");
    if (typeof raw.card !== "string" || typeof raw.action !== "string") return null;
    return { action: raw.action, id: typeof raw.id === "string" ? raw.id : "", card: raw.card };
  } catch {
    return null;
  }
}

const DONE = new Set(["approved_unchanged", "approved_with_edit", "executed"]);

export default function ChatAppActionCard({ id }: { id: number }) {
  const router = useRouter();
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

  if (!instance || instance.decision_class !== "app_action") return null;
  const payload = readPayload(instance);
  if (!payload) return null;
  const done = DONE.has(instance.status);
  const waiting = instance.status === "proposed";
  if (!done && !waiting) return null;

  const confirm = async () => {
    setBusy("Doing it…");
    setError(null);
    try {
      const updated = await approveAppAction(instance.id);
      setInstance(updated);
      // Deleted the chat this card is in: there is nothing left to show here.
      if (payload.action === "delete_chat" && payload.id === instance.originating_session_id) {
        router.push("/?new=1");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "That didn't go through.");
    } finally {
      setBusy(null);
    }
  };

  const cancel = async () => {
    setBusy("Cancelling…");
    setError(null);
    try {
      setInstance(await rejectDecision(instance.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't cancel it.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Needs your OK</div>
      <div className="mt-1 text-base sm:text-[17px] font-semibold leading-snug text-fg break-words whitespace-pre-line">
        {payload.card}
      </div>
      {done ? (
        <p className="mt-3 text-sm font-semibold text-emerald-700 dark:text-emerald-300">✓ Done.</p>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => void confirm()} disabled={busy !== null}>
            {busy ?? "Confirm"}
          </Button>
          <Button variant="ghost" className="ml-auto" onClick={() => void cancel()} disabled={busy !== null}>
            Cancel
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}
