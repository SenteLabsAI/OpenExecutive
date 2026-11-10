"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import Icon from "@/components/Icon";
import AboutYouCard from "@/components/settings/AboutYouCard";
import {
  forgetReflectionLesson,
  forgetWorkedOut,
  getHistory,
  getReflection,
  listStandingFacts,
  listWorkedOut,
  retireStandingFact,
  type ReflectionLesson,
  type ReflectionState,
  type StandingFact,
  type WorkedOutFigure,
} from "@/lib/api";
import type { HistoryNote } from "@/lib/history";
import { lessonUseLine, totalUses, weekUseLine } from "@/lib/lessonUse";
import type { PulseData } from "./PulseHeader";
import { NewSinceCard } from "./MemoryChanges";
import { Skeleton, formatDate } from "./shared";

// Memory → What it knows: three lists. About you (what it learned from
// talking with you, and what you said); About the company (what you told it,
// what it worked out and the projects it tracks, in one list that says where
// each line came from); What I learned (the lessons, and where each was
// used). The merge is on this screen only: each kind keeps its own store,
// its own route and its own full screen (`?tab=`), where it is edited.

const PREVIEW_NOTES = 3;
const PREVIEW_COMPANY = 6;
const PREVIEW_LESSONS = 5;
const COULD_NOT_LOAD = "Couldn't load this list. Try again in a moment.";

type Source = "told" | "worked" | "project";

interface CompanyRow {
  key: string;
  source: Source;
  title: string;
  detail: string;
  at: string;
  /** The full screen where this kind is edited. */
  href: string;
  tag: string;
  remove?: () => Promise<void>;
}

const TAG_STYLE: Record<Source, string> = {
  told: "bg-accent/10 text-accent",
  worked: "bg-emerald-500/10 text-emerald-600",
  project: "bg-surface-overlay text-fg-muted",
};

/** "Nov 9", or with the year when it isn't this one. */
function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return formatDate(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function factDetail(f: StandingFact): string {
  const by = f.recorded_by_role === "teammate" ? ` · per ${f.recorded_by_name || "a teammate"}` : "";
  if (f.status === "proposed") return `Waiting for your OK${by}`;
  const kind = f.decision ? (f.outcome ? `Decision · outcome: ${f.outcome}` : "Decision") : f.subject;
  return `${kind} · ${shortDate(f.created_at)}${by}`;
}

function Card({ id, title, description, flag, children }: {
  id?: string;
  title: string;
  description: string;
  flag?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section id={id} className="rounded-2xl border border-line bg-surface-elevated p-5 sm:p-6 min-w-0 scroll-mt-20">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base sm:text-lg font-semibold text-fg">{title}</h2>
          <p className="mt-1 text-sm text-fg-muted leading-relaxed">{description}</p>
        </div>
        {flag}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Loading() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-5 w-3/4" />
      <Skeleton className="h-5 w-1/2" />
    </div>
  );
}

/** "Remove", then "Remove it" / "Keep": one tap never removes anything. */
function RemoveButton({ what, onRemove }: { what: string; onRemove: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const run = async () => {
    setBusy(true);
    setFailed(false);
    try {
      await onRemove();
    } catch {
      setFailed(true);
      setBusy(false);
      setConfirming(false);
    }
  };
  const quiet = "min-h-touch px-2 text-sm font-medium rounded-lg hover:bg-surface-overlay transition-colors";
  if (!confirming) {
    return (
      <button type="button" aria-label={`Remove ${what}`} className={`${quiet} text-fg-muted`} onClick={() => setConfirming(true)}>
        {failed ? "Try again" : "Remove"}
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <button type="button" className={`${quiet} text-red-500`} disabled={busy} onClick={() => void run()}>
        {busy ? "Removing…" : "Remove it"}
      </button>
      <button type="button" className={`${quiet} text-fg-muted`} disabled={busy} onClick={() => setConfirming(false)}>
        Keep
      </button>
    </span>
  );
}

function MoreLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="mt-2 inline-flex h-10 items-center gap-1 rounded-xl px-2 -ml-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
    >
      {children}
      <Icon name="chevron-right" size="w-4 h-4" />
    </Link>
  );
}

function MoreButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mt-2 inline-flex h-10 items-center gap-1 rounded-xl px-2 -ml-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
    >
      {children}
    </button>
  );
}

export default function KnowsOverview({ pulse, lastVisit }: { pulse: PulseData; lastVisit: string | null }) {
  const { data } = pulse;
  const [facts, setFacts] = useState<{ rows: StandingFact[]; toApprove: number; retirable: Set<number> } | null>(null);
  // undefined while loading; null when this viewer keeps none.
  const [figures, setFigures] = useState<WorkedOutFigure[] | null | undefined>(undefined);
  const [reflection, setReflection] = useState<ReflectionState | null | undefined>(undefined);
  // A list that couldn't be read says so, rather than looking empty.
  const [failed, setFailed] = useState<{ facts?: boolean; figures?: boolean }>({});
  const [filter, setFilter] = useState<Source | "all">("all");
  const [allCompany, setAllCompany] = useState(false);
  const [allLessons, setAllLessons] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listStandingFacts()
      .then((page) => {
        if (cancelled) return;
        const proposed = page.can_review ? page.facts.filter((f) => f.status === "proposed") : [];
        const active = page.facts.filter((f) => f.status === "active" && f.kind !== "profile");
        setFacts({ rows: [...proposed, ...active], toApprove: proposed.length, retirable: new Set(page.retirable_ids) });
      })
      .catch(() => {
        if (cancelled) return;
        setFailed((f) => ({ ...f, facts: true }));
        setFacts({ rows: [], toApprove: 0, retirable: new Set() });
      });
    listWorkedOut()
      .then((rows) => {
        if (!cancelled) setFigures(rows);
      })
      .catch(() => {
        if (cancelled) return;
        setFailed((f) => ({ ...f, figures: true }));
        setFigures([]);
      });
    getReflection()
      .then((state) => {
        if (!cancelled) setReflection(state);
      })
      .catch(() => {
        if (!cancelled) setReflection(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const lessons: ReflectionLesson[] | null | undefined = useMemo(
    () =>
      reflection
        ? reflection.lessons
            .filter((l) => l.retired_at === null)
            .sort((a, b) => b.created_at.localeCompare(a.created_at))
        : reflection,
    [reflection],
  );

  const company: CompanyRow[] | null = useMemo(() => {
    if (!data || facts === null || figures === undefined) return null;
    const rows: CompanyRow[] = [];
    for (const f of facts.rows) {
      const mine = f.recorded_by_role !== "teammate";
      rows.push({
        key: `fact-${f.id}`,
        source: "told",
        title: f.statement,
        detail: factDetail(f),
        at: f.created_at,
        href: "/memories?tab=corrections",
        tag: mine ? "You told me" : `${(f.recorded_by_name || "A teammate").split(" ")[0]} told me`,
        remove:
          f.status === "active" && facts.retirable.has(f.id)
            ? async () => {
                await retireStandingFact(f.id, "removed on the Memory page");
                setFacts((prev) => (prev ? { ...prev, rows: prev.rows.filter((r) => r.id !== f.id) } : prev));
              }
            : undefined,
      });
    }
    for (const f of figures ?? []) {
      const from = f.source ? `From ${f.source} · ` : "";
      rows.push({
        key: `figure-${f.id}`,
        source: "worked",
        title: `${f.subject}: ${f.figure}`,
        detail: `${from}${shortDate(f.as_of || f.created_at)} · only you see this`,
        at: f.created_at,
        href: "/memories?tab=worked_out",
        tag: "I worked it out",
        remove: async () => {
          await forgetWorkedOut(f.id);
          setFigures((prev) => (prev ? prev.filter((r) => r.id !== f.id) : prev));
        },
      });
    }
    for (const p of data.initiatives) {
      const at = p.updated_at ?? p.created_at;
      rows.push({
        key: `project-${p.id}`,
        source: "project",
        title: p.title,
        detail: `${p.status.charAt(0).toUpperCase()}${p.status.slice(1)} · ${shortDate(at)}`,
        at: at ?? "",
        href: "/memories?tab=initiatives",
        tag: "Project",
      });
    }
    // The newest first, whatever kind it is.
    return rows.sort((a, b) => b.at.localeCompare(a.at));
  }, [data, facts, figures]);

  const counts = useMemo(() => {
    const c = { told: 0, worked: 0, project: 0 };
    for (const r of company ?? []) c[r.source] += 1;
    return c;
  }, [company]);
  const shownCompany = (company ?? []).filter((r) => filter === "all" || r.source === filter);
  const olderAdvice = data ? data.advice.length : 0;
  // Whose chat a lesson came from matters only when there is more than one person.
  const severalPeople = new Set((lessons ?? []).map((l) => l.person_id)).size > 1;

  const learned = reflection?.learned_this_week ?? 0;
  const used = totalUses(reflection?.used_this_week);
  const usedWhere = weekUseLine(reflection?.used_this_week);

  const chip = (id: Source | "all", label: string, n: number) => (
    <button
      key={id}
      type="button"
      aria-pressed={filter === id}
      onClick={() => {
        setFilter(id);
        setAllCompany(false);
      }}
      className={`h-10 rounded-xl border px-3 text-sm font-medium transition-colors ${
        filter === id
          ? "border-accent bg-accent/10 text-accent"
          : "border-line text-fg-muted hover:bg-surface-overlay"
      }`}
    >
      {label} {n}
    </button>
  );

  return (
    <div className="space-y-4">
      {reflection && (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-2xl bg-accent/10 px-5 py-3.5 text-[15px] text-fg">
          <p className="min-w-0">
            <span className="font-semibold">
              {learned === 0
                ? "Nothing new learned this week"
                : `This week I learned ${learned} ${learned === 1 ? "thing" : "things"}`}
            </span>
            {used > 0 && ` and used what I know ${used} ${used === 1 ? "time" : "times"}: ${usedWhere}.`}
            {used === 0 && "."}
          </p>
          <a href="#what-i-learned" className="shrink-0 text-sm font-semibold text-accent hover:underline">
            See what
          </a>
        </div>
      )}
      <NewSinceCard since={lastVisit} />

      <AboutYouBlock />

      <Card
        title="About the company"
        description="Everything it knows about the business, in one list. Each line says where it came from. Used in every answer."
        flag={
          facts && facts.toApprove > 0 ? (
            <Link
              href="/memories?tab=corrections"
              className="shrink-0 rounded-full bg-amber-500/15 px-2.5 py-0.5 text-xs font-semibold text-amber-500"
            >
              {facts.toApprove} to approve
            </Link>
          ) : undefined
        }
      >
        {company === null ? (
          <Loading />
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              {chip("all", "All", company.length)}
              {chip("told", "You told me", counts.told)}
              {figures !== null && chip("worked", "I worked it out", counts.worked)}
              {chip("project", "Projects", counts.project)}
            </div>
            {(failed.facts || failed.figures) && <p className="mt-3 text-sm text-fg-muted">{COULD_NOT_LOAD}</p>}
            {shownCompany.length === 0 ? (
              <p className="mt-3 text-[15px] text-fg-muted">
                Nothing yet. Tell it a fact or a correction in chat and it keeps it.
              </p>
            ) : (
              <ul className="mt-1">
                {(allCompany ? shownCompany : shownCompany.slice(0, PREVIEW_COMPANY)).map((r) => (
                  <li key={r.key} className="flex items-start gap-3 py-3 border-t border-line first:border-t-0 min-w-0">
                    <Link href={r.href} className="min-w-0 flex-1 group" title="Open to edit">
                      <div className="text-[15px] font-semibold text-fg break-words line-clamp-2 group-hover:underline">{r.title}</div>
                      <div className="text-sm text-fg-muted mt-0.5 break-words">{r.detail}</div>
                    </Link>
                    <div className="shrink-0 flex flex-wrap items-center justify-end gap-1">
                      <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap ${TAG_STYLE[r.source]}`}>
                        {r.tag}
                      </span>
                      {r.remove && <RemoveButton what={r.title} onRemove={r.remove} />}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {!allCompany && shownCompany.length > PREVIEW_COMPANY && (
              <MoreButton onClick={() => setAllCompany(true)}>All {shownCompany.length} ›</MoreButton>
            )}
          </>
        )}
      </Card>

      {lessons !== null && (
        <Card
          id="what-i-learned"
          title="What I learned"
          description="Lessons from our chats and your corrections. Each one is used everywhere: chat, 1:1s and email."
        >
          {lessons === undefined ? (
            <Loading />
          ) : lessons.length === 0 ? (
            <p className="text-[15px] text-fg-muted">Nothing yet. What I learn from our chats shows up here.</p>
          ) : (
            <ul>
              {(allLessons ? lessons : lessons.slice(0, PREVIEW_LESSONS)).map((l) => (
                <li key={l.id} className="flex items-start gap-3 py-3 border-t border-line first:border-t-0 min-w-0">
                  <div className="min-w-0 flex-1">
                    <div className="text-[15px] font-semibold text-fg break-words">{l.text}</div>
                    <div className="text-sm text-fg-muted mt-0.5 break-words">
                      Learned {shortDate(l.created_at)} from a chat
                      {severalPeople && l.person_name ? ` with ${l.person_name}` : ""} · {lessonUseLine(l.used_in)}
                    </div>
                  </div>
                  <RemoveButton
                    what={l.text}
                    onRemove={async () => {
                      await forgetReflectionLesson(l.id);
                      setReflection((prev) =>
                        prev ? { ...prev, lessons: prev.lessons.filter((x) => x.id !== l.id) } : prev,
                      );
                    }}
                  />
                </li>
              ))}
            </ul>
          )}
          <div className="flex flex-wrap items-center gap-x-4">
            {!allLessons && lessons !== undefined && lessons.length > PREVIEW_LESSONS && (
              <MoreButton onClick={() => setAllLessons(true)}>All {lessons.length} ›</MoreButton>
            )}
            <MoreLink href="/memories?tab=reflection">How it&apos;s going</MoreLink>
          </div>
        </Card>
      )}

      {olderAdvice > 0 && (
        <Link href="/memories?tab=advice" className="inline-flex min-h-touch items-center px-1 text-sm text-fg-muted hover:underline">
          Older advice ({olderAdvice})
        </Link>
      )}
    </div>
  );
}

/** About you: what it learned from talking with you, with what you said
 *  under it in the same card. The caller's own, so anyone on the team may
 *  be shown this block. */
export function AboutYouBlock() {
  const [notes, setNotes] = useState<HistoryNote[] | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    getHistory()
      .then((state) => {
        if (!cancelled) setNotes(state ? state.notes : null);
      })
      .catch(() => {
        if (!cancelled) setNotes(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <AboutYouCard
      title="About you"
      description="Learned from talking with you. Remove any line that's wrong. Only you see this."
      footer={
        notes ? (
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">What you said · {notes.length}</h3>
            {notes.length === 0 ? (
              <p className="mt-2 text-[15px] text-fg-muted">
                Nothing yet. What you say in chat and in replies you approve shows up here.
              </p>
            ) : (
              <ul className="mt-2 rounded-xl border border-line divide-y divide-line">
                {notes.slice(0, PREVIEW_NOTES).map((n) => (
                  <li key={n.id} className="flex gap-4 px-4 py-3 min-w-0">
                    <span className="shrink-0 w-24 text-xs text-fg-muted pt-1">{formatDate(n.occurred_at || n.created_at)}</span>
                    <span className="min-w-0 text-[15px] text-fg break-words">{n.correction || n.summary}</span>
                  </li>
                ))}
              </ul>
            )}
            <MoreLink href="/memories?tab=history">{notes.length > PREVIEW_NOTES ? `All ${notes.length}` : "Open"}</MoreLink>
          </div>
        ) : undefined
      }
    />
  );
}
