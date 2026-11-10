"use client";

import Link from "next/link";
import { useState } from "react";

import { Spinner } from "@/components/ui/Spinner";
import { getGoalActivity, type GoalWeek as GoalWeekData } from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";

// "What I did for this goal this week" under every goal on /goals
// (GET /goals/week, departments/goal_week.py). Always shown: "Nothing this
// week." when there was nothing, a "Waiting on you" line per step held for
// a yes (opens its card on Home), and See everything for the last 90 days.

export const GOAL_WEEK_HEADING = "What I did for this goal this week";

export default function GoalWeek({ goalId, week }: { goalId: number; week: GoalWeekData | undefined }) {
  const [all, setAll] = useState<GoalWeekData | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const done = week?.done ?? [];
  const waiting = week?.waiting ?? [];

  async function seeEverything() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (all) return;
    setLoading(true);
    setErr(null);
    try {
      setAll(await getGoalActivity(goalId));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't load it");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-3 pt-3 border-t border-dashed border-line">
      <div className="text-[15px] font-semibold text-fg">{GOAL_WEEK_HEADING}</div>
      {week === undefined ? (
        <p className="text-[15px] text-fg-subtle mt-1">…</p>
      ) : (
        <ul className="mt-1 space-y-1 text-[15px] text-fg-muted">
          {done.length === 0 && <li>Nothing this week.</li>}
          {done.map((line, i) => (
            <li key={`d${i}`} className="flex gap-2">
              <span aria-hidden="true">•</span>
              <span className="min-w-0">{line.text}</span>
            </li>
          ))}
          {waiting.map((line, i) => (
            <li key={`w${i}`} className="flex gap-2">
              <span aria-hidden="true">•</span>
              <span className="min-w-0">
                <span className="text-fg">Waiting on you: </span>
                {line.card_id !== null ? (
                  <Link href={`/?card=${line.card_id}`} className="text-accent hover:underline">
                    {line.text}
                  </Link>
                ) : (
                  line.text
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        onClick={() => void seeEverything()}
        aria-expanded={open}
        className="mt-2 text-[15px] font-medium text-accent hover:underline cursor-pointer"
      >
        {open ? "Hide" : "See everything"}
      </button>
      {open && (
        <div className="mt-2 rounded-xl bg-surface px-3 py-2">
          {loading && <Spinner />}
          {err && <p className="text-sm text-rose-500">{err}</p>}
          {all && all.done.length === 0 && all.waiting.length === 0 && (
            <p className="text-sm text-fg-muted">Nothing in the last 90 days.</p>
          )}
          {all && (all.done.length > 0 || all.waiting.length > 0) && (
            <ul className="space-y-1.5 text-sm text-fg-muted">
              {all.waiting.map((line, i) => (
                <li key={`aw${i}`}>
                  <span className="text-fg">Waiting on you: </span>
                  {line.text} <span className="text-fg-subtle">· {formatRelativeTime(line.at)}</span>
                </li>
              ))}
              {all.done.map((line, i) => (
                <li key={`ad${i}`}>
                  {line.text} <span className="text-fg-subtle">· {formatRelativeTime(line.at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
