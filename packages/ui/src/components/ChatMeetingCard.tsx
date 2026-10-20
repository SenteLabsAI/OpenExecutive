"use client";

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  approveDecision,
  getDecision,
  rejectDecision,
  type DecisionInstance,
} from "@/lib/api";

// The chat: a meeting the Executive proposed while you were chatting, shown
// under its reply with Approve, Edit and Dismiss, so you never have to go to
// Today for it. The same card also waits on the briefing; answering either
// clears both (the server clears the briefing alert). Only the person who
// approves it gets an id on the chip (action_chips), and the server checks
// again on Approve.

interface MeetingPayload {
  title: string;
  start: string;
  end: string;
  attendee_emails: string[];
}

function readPayload(instance: DecisionInstance): MeetingPayload | null {
  try {
    const raw = JSON.parse(instance.final_payload_json || instance.proposed_payload_json || "{}");
    if (typeof raw.title !== "string" || typeof raw.start !== "string" || typeof raw.end !== "string") {
      return null;
    }
    const emails = Array.isArray(raw.attendee_emails)
      ? raw.attendee_emails.filter((e: unknown): e is string => typeof e === "string")
      : [];
    return { title: raw.title, start: raw.start, end: raw.end, attendee_emails: emails };
  } catch {
    return null;
  }
}

export function formatMeetingWhen(start: string, end: string): string {
  const s = new Date(start);
  const e = new Date(end);
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return "";
  const day = s.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  const from = s.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const to = e.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZoneName: "short" });
  return `${day} · ${from}–${to}`;
}

// <input type="datetime-local"> wants local wall time with no zone.
function toLocalInput(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(value: string): string | null {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

const APPROVED = new Set(["approved_unchanged", "approved_with_edit", "executed"]);

export default function ChatMeetingCard({ id }: { id: number }) {
  const [instance, setInstance] = useState<DecisionInstance | null>(null);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getDecision(id, controller.signal)
      .then(setInstance)
      .catch(() => { /* not this person's to see, or gone: the chip stays */ });
    return () => controller.abort();
  }, [id]);

  if (!instance || instance.decision_class !== "meeting_scheduling") return null;
  const payload = readPayload(instance);
  if (!payload) return null;
  const approved = APPROVED.has(instance.status);
  const waiting = instance.status === "proposed";
  // Dismissed, cancelled or failed: the reply's own words say what happened.
  if (!approved && !waiting) return null;

  const openEdit = () => {
    setTitle(payload.title);
    setStart(toLocalInput(payload.start));
    setEnd(toLocalInput(payload.end));
    setError(null);
    setEditing(true);
  };

  const approve = async () => {
    let edits: Record<string, unknown> | undefined;
    if (editing) {
      const s = fromLocalInput(start);
      const e = fromLocalInput(end);
      if (!title.trim() || !s || !e) {
        setError("Give it a title, a start and an end.");
        return;
      }
      if (new Date(e) <= new Date(s)) {
        setError("The end has to be after the start.");
        return;
      }
      edits = {};
      if (title.trim() !== payload.title) edits.title = title.trim();
      if (s !== new Date(payload.start).toISOString()) edits.start = s;
      if (e !== new Date(payload.end).toISOString()) edits.end = e;
      if (Object.keys(edits).length === 0) edits = undefined;
    }
    setBusy("Booking…");
    setError(null);
    try {
      setInstance(await approveDecision(instance.id, edits));
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't book it.");
    } finally {
      setBusy(null);
    }
  };

  const dismiss = async () => {
    setBusy("Dismissing…");
    setError(null);
    try {
      setInstance(await rejectDecision(instance.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't dismiss it.");
    } finally {
      setBusy(null);
    }
  };

  const who = payload.attendee_emails.length > 0 ? payload.attendee_emails.join(", ") : "No one invited";
  return (
    <article className="mt-3 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="text-xs font-semibold uppercase tracking-wide text-accent">Meeting</div>
      {editing ? (
        <div className="mt-2 space-y-2">
          <label className="block text-sm text-fg-muted">
            Title
            <input
              className="mt-1 block w-full rounded-lg border border-line bg-surface px-3 py-2 text-base text-fg"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={busy !== null}
            />
          </label>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-sm text-fg-muted">
              Starts
              <input
                type="datetime-local"
                className="mt-1 block w-full rounded-lg border border-line bg-surface px-3 py-2 text-base text-fg"
                value={start}
                onChange={(e) => setStart(e.target.value)}
                disabled={busy !== null}
              />
            </label>
            <label className="block text-sm text-fg-muted">
              Ends
              <input
                type="datetime-local"
                className="mt-1 block w-full rounded-lg border border-line bg-surface px-3 py-2 text-base text-fg"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
                disabled={busy !== null}
              />
            </label>
          </div>
        </div>
      ) : (
        <>
          <div className="mt-1 text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
            {payload.title}
          </div>
          <div className="mt-0.5 text-sm text-fg-muted break-words">
            {formatMeetingWhen(payload.start, payload.end)} · {who}
          </div>
        </>
      )}
      {approved ? (
        <p className="mt-3 text-sm font-semibold text-emerald-700 dark:text-emerald-300">
          ✓ Booked. It&apos;s on your calendar.
        </p>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => void approve()} disabled={busy !== null}>
            {busy ?? "Approve"}
          </Button>
          {editing ? (
            <Button variant="secondary" onClick={() => setEditing(false)} disabled={busy !== null}>
              Cancel edit
            </Button>
          ) : (
            <Button variant="secondary" onClick={openEdit} disabled={busy !== null}>
              Edit
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy !== null}>
            Dismiss
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}
