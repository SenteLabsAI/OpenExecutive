"use client";

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  getPersonReplyHabits,
  keepPersonReplyHabit,
  updatePerson,
  type Person,
  type ReplyHabitSuggestion,
} from "@/lib/api";

// "From how <first name> answers" on a person's Overview: a reply time and
// a channel suggested from how they really answered (GET
// /people/{id}/reply-habits, people/reply_habits.py). Use it writes the
// field as Edit would; Keep mine hides that suggestion until the evidence
// changes. Nothing changes on its own. Not shown when there is nothing to
// suggest, or to anyone who can't edit the person.

export default function ReplyHabits({
  person,
  onUpdated,
}: {
  person: Person;
  onUpdated: (p: Person) => void;
}) {
  const [items, setItems] = useState<ReplyHabitSuggestion[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const id = person.id;

  useEffect(() => {
    if (id == null) return;
    getPersonReplyHabits(id).then(setItems, () => setItems([]));
  }, [id, person.response_sla_hours, person.preferred_channel]);

  if (id == null || items.length === 0) return null;
  const first = (person.full_name || "").trim().split(/\s+/)[0] || "them";

  async function act(s: ReplyHabitSuggestion, use: boolean) {
    if (id == null) return;
    setBusy(s.kind);
    setErr(null);
    try {
      if (use) {
        const updated = await updatePerson(
          id,
          s.kind === "reply_time"
            ? { response_sla_hours: Number(s.value) }
            : { preferred_channel: s.value },
        );
        onUpdated(updated);
      } else {
        await keepPersonReplyHabit(id, s.kind);
      }
      setItems((xs) => xs.filter((x) => x.kind !== s.kind));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That didn't save");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-3 rounded-xl border border-accent/30 bg-accent/5 px-4 py-3">
      <p className="text-[15px] font-semibold text-fg">From how {first} answers</p>
      <ul className="mt-1 divide-y divide-line/60">
        {items.map((s) => (
          <li key={s.kind} className="flex flex-col gap-2 py-2.5 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-[15px] font-medium text-fg">{s.headline}</p>
              <p className="text-sm text-fg-muted">{s.evidence}</p>
            </div>
            <div className="flex gap-2 flex-shrink-0">
              <Button variant="primary" disabled={busy !== null} onClick={() => void act(s, true)}>
                Use it
              </Button>
              <Button disabled={busy !== null} onClick={() => void act(s, false)}>
                Keep mine
              </Button>
            </div>
          </li>
        ))}
      </ul>
      {err && <p className="text-sm text-rose-500 mt-1">{err}</p>}
    </div>
  );
}
