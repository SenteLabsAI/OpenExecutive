"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import InfoTip from "@/components/InfoTip";
import {
  closeOpenLoop,
  deleteInitiative,
  getPersonOpenLoops,
  getTopThree,
  getWeeklyReview,
  listInitiatives,
  updateInitiative,
  type Initiative,
  type OpenLoop,
  type TopThreeToday,
  type WeeklyReviewSummary,
} from "@/lib/api";
import { dueSoon, loopText } from "@/lib/dueSoon";
import { reviewExcerpt, reviewRanLabel, topThreeSlot, topThreeWhy } from "@/lib/rhythmCards";

import { PanelIntro, RankBadge, TONE_TEXT } from "./shared";

// Solo-only parts of Home. Each loads its own data through a hook here, so
// the tile row can count it before its side panel is opened.

// ── Your projects ─────────────────────────────────────────────────────────
// The projects (initiatives) the Executive is tracking as active — the solo
// stand-in for Departments. "Done" marks one completed (PATCH), which is what
// takes it out of the Executive's active context; "Drop" deletes it after a
// confirm, since there is no "dropped" status the rest of the system treats
// as closed.
export function useActiveProjects(enabled: boolean) {
  const [projects, setProjects] = useState<Initiative[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    listInitiatives()
      .then((all) => {
        if (!cancelled) setProjects(all.filter((i) => i.status === "active"));
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't load your projects.");
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return { projects, setProjects, error, setError };
}

export function ProjectsPanelBody({ state }: { state: ReturnType<typeof useActiveProjects> }) {
  const { projects, setProjects, error, setError } = state;
  const [busyId, setBusyId] = useState<number | null>(null);
  // Drop asks first, in the row itself.
  const [confirmDrop, setConfirmDrop] = useState<number | null>(null);

  const close = async (project: Initiative, how: "done" | "drop") => {
    setBusyId(project.id);
    setError(null);
    try {
      if (how === "done") await updateInitiative(project.id, { status: "completed" });
      else await deleteInitiative(project.id);
      setProjects((prev) => (prev ?? []).filter((p) => p.id !== project.id));
    } catch {
      setError(how === "done" ? "Couldn't mark it done — try again." : "Couldn't drop it — try again.");
    } finally {
      setBusyId(null);
      setConfirmDrop(null);
    }
  };

  return (
    <>
      <PanelIntro>
        The projects you&apos;re running that the Executive is keeping track of. Mention a new one in chat and it
        appears here.
      </PanelIntro>
      {error && <p className={`mb-2 text-sm ${TONE_TEXT.rose}`}>{error}</p>}
      {projects === null ? (
        !error && <p className="py-2 text-[15px] text-fg-muted">Loading…</p>
      ) : projects.length === 0 ? (
        <p className="py-2 text-[15px] text-fg-muted">
          No active projects. Tell the Executive about one you&apos;re running and it will keep track of it here.
        </p>
      ) : (
        <div className="divide-y divide-line">
          {projects.map((p) => (
            <div key={p.id} className="py-3.5">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-[15px] font-medium text-fg break-words">{p.title}</div>
                  {p.summary && (
                    <div className="mt-0.5 text-sm text-fg-muted line-clamp-2" title={p.summary}>
                      {p.summary}
                    </div>
                  )}
                </div>
                {confirmDrop !== p.id && (
                  <div className="flex flex-shrink-0 items-center gap-1.5">
                    <Button size="sm" variant="secondary" onClick={() => void close(p, "done")} disabled={busyId !== null}>
                      Done
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirmDrop(p.id)} disabled={busyId !== null}>
                      Drop
                    </Button>
                  </div>
                )}
              </div>
              {confirmDrop === p.id && (
                <div role="alert" className="mt-2 rounded-xl border border-rose-500/30 bg-rose-500/5 px-3 py-2.5">
                  <p className="text-sm text-fg">
                    Drop &ldquo;{p.title}&rdquo;? The Executive stops tracking it, and it is removed from Pulse.
                  </p>
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" variant="danger" onClick={() => void close(p, "drop")} disabled={busyId !== null}>
                      {busyId === p.id ? "Dropping…" : "Drop it"}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirmDrop(null)} disabled={busyId !== null}>
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ── Due soon ──────────────────────────────────────────────────────────────
// What you own that is due within a week or overdue — what you promised by a
// date and what others asked of you. Read from your open loops
// (GET /people/{id}/open-loops); "Done" closes one (POST
// /open-loops/{id}/close), which also stops the Executive reminding you.
export function useOpenLoops(principalId: number | null) {
  const [loops, setLoops] = useState<OpenLoop[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (principalId == null) return;
    let cancelled = false;
    getPersonOpenLoops(principalId)
      .then((all) => {
        if (!cancelled) setLoops(all);
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't load what's due.");
      });
    return () => {
      cancelled = true;
    };
  }, [principalId]);

  return { loops, setLoops, error, setError, view: loops === null ? null : dueSoon(loops) };
}

export function DueSoonPanelBody({ state }: { state: ReturnType<typeof useOpenLoops> }) {
  const { setLoops, error, setError, view } = state;
  const [busyId, setBusyId] = useState<number | null>(null);

  const markDone = async (loopId: number) => {
    setBusyId(loopId);
    setError(null);
    try {
      await closeOpenLoop(loopId, "done");
      setLoops((prev) => (prev ?? []).filter((l) => l.loop_id !== loopId));
    } catch {
      setError("Couldn't mark it done — try again.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <PanelIntro>
        What you&apos;ve promised by a date, and what others have asked of you, due in the next week. Tell the
        Executive &ldquo;I&apos;ll send it by Friday&rdquo; and it shows up here; the Executive reminds you when
        it&apos;s due.
      </PanelIntro>
      {error && <p className={`mb-2 text-sm ${TONE_TEXT.rose}`}>{error}</p>}
      {view === null ? (
        !error && <p className="py-2 text-[15px] text-fg-muted">Loading…</p>
      ) : view.items.length === 0 ? (
        <p className="py-2 text-[15px] text-fg-muted">
          Nothing due this week.
          {view.later > 0 && ` ${view.later} due later.`}
        </p>
      ) : (
        <>
          <div className="divide-y divide-line">
            {view.items.map((item) => (
              <div key={item.loop.loop_id} className="flex items-start gap-3 py-3.5">
                <div className="min-w-0 flex-1">
                  <div className="text-[15px] text-fg break-words">{item.text}</div>
                  <div className={`mt-0.5 text-sm ${item.overdue ? TONE_TEXT.amber : "text-fg-muted"}`}>
                    {item.dueLabel}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void markDone(item.loop.loop_id)}
                  disabled={busyId !== null}
                >
                  {busyId === item.loop.loop_id ? "Closing…" : "Done"}
                </Button>
              </div>
            ))}
          </div>
          {view.later > 0 && <p className="pt-2 text-sm text-fg-muted">{view.later} more due later.</p>}
        </>
      )}
    </>
  );
}

// ── This week's review ────────────────────────────────────────────────────
// The latest weekly review (GET /today/weekly-review) — when it ran and next
// week's top three, with a link to the whole review on its run page. Null
// until a review has run, and for anyone but the owner.
export function useWeeklyReview(enabled: boolean) {
  const [review, setReview] = useState<WeeklyReviewSummary | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    getWeeklyReview(controller.signal)
      .then(setReview)
      .catch(() => { /* no card is better than a broken one */ });
    return () => controller.abort();
  }, [enabled]);
  return review;
}

export function weeklyReviewMeta(review: WeeklyReviewSummary): string {
  return [review.period, reviewRanLabel(review.completed_at)].filter(Boolean).join(" · ");
}

export function WeeklyReviewPanelBody({ review }: { review: WeeklyReviewSummary }) {
  const excerpt = reviewExcerpt(review);
  const meta = weeklyReviewMeta(review);
  return (
    <>
      <PanelIntro>
        Once a week the Executive looks back on your week — your goals by area, what&apos;s due, projects that went
        quiet and the decisions you made — and picks next week&apos;s top three. It&apos;s also sent to you when a
        chat app or email is connected.
      </PanelIntro>
      {meta && <p className="mb-3 text-sm text-fg-muted">{meta}</p>}
      {excerpt.heading && (
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-fg-muted">{excerpt.heading}</div>
      )}
      {excerpt.numbered ? (
        <ol className="divide-y divide-line">
          {excerpt.lines.map((line, i) => (
            <li key={i} className="flex items-start gap-3 py-3">
              <RankBadge n={i + 1} />
              <span className="min-w-0 flex-1 text-[15px] text-fg break-words">{line}</span>
            </li>
          ))}
        </ol>
      ) : (
        excerpt.lines.map((line, i) => (
          <p key={i} className="py-0.5 text-[15px] text-fg-muted break-words">
            {line}
          </p>
        ))
      )}
      <Link
        href={`/jobs/runs/${encodeURIComponent(review.run_id)}`}
        className="mt-4 inline-block text-sm font-medium text-accent hover:underline"
      >
        Read the review →
      </Link>
    </>
  );
}

// ── Top three today ───────────────────────────────────────────────────────
// The three things to focus on today — the same pick, order and free slots
// as the morning brief (GET /today/top-three). Solo's main job, so it stays
// on Home as a short list under Needs you. Loads on its own after the
// briefing, since it may wait on the calendar (up to 4 s), and hides when
// there is nothing to pick or the viewer isn't the owner (the API returns
// null). `ownerName` turns a commitment's stored text into "you" wording.
export function TopThreeList({ ownerName, id }: { ownerName: string; id?: string }) {
  const [top, setTop] = useState<TopThreeToday | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getTopThree(controller.signal)
      .then(setTop)
      .catch(() => { /* no card is better than a broken one */ });
    return () => controller.abort();
  }, []);

  if (!top || top.items.length === 0) return null;
  return (
    <section id={id}>
      <div className="mb-3 flex items-center gap-1.5">
        <h2 className="text-lg font-semibold text-fg">Top three today</h2>
        <InfoTip align="left">
          The three things to focus on today, picked from what&apos;s overdue or due, goals that are slipping and
          your active projects — the same three your morning brief opens with. When the Executive can read your
          calendar, each gets a free slot in today&apos;s working hours.
        </InfoTip>
      </div>
      <ol className="divide-y divide-line rounded-2xl border border-line bg-surface-elevated px-4 sm:px-5">
        {top.items.map((item, i) => {
          const text =
            item.kind === "commitment" && ownerName
              ? loopText({ description: item.text, owner_name: ownerName })
              : item.text;
          const slot = topThreeSlot(item);
          return (
            <li key={item.key} className="flex items-start gap-3 py-3.5">
              <RankBadge n={i + 1} />
              <div className="min-w-0 flex-1">
                <div className="text-[15px] text-fg break-words">{text}</div>
                <div className="mt-0.5 text-sm text-fg-muted">{topThreeWhy(item)}</div>
              </div>
              {slot && (
                <span className={`mt-0.5 flex-shrink-0 text-sm tabular-nums ${item.slot ? "text-accent" : "text-fg-muted"}`}>
                  {slot}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
