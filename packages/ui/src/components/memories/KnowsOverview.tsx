"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import Icon from "@/components/Icon";
import AboutYouCard from "@/components/settings/AboutYouCard";
import {
  getHistory,
  getReflection,
  listStandingFacts,
  listWorkedOut,
  type StandingFact,
  type WorkedOutFigure,
} from "@/lib/api";
import type { PulseData } from "./PulseHeader";
import { NewSinceCard } from "./MemoryChanges";
import { figureSourceLine } from "./WorkedOutTab";
import { Skeleton, formatDate } from "./shared";

// Memory → What it knows: four groups, each a few rows and a link to its full
// list. About you (what it learned from talking with you, and your notes);
// About the company (the projects it tracks); What you taught it (facts,
// corrections and decisions, used in every answer); What it worked out (its
// own figures, with where they came from). Learning sits under them.

const PREVIEW_ROWS = 3;
const COULD_NOT_LOAD = "Couldn't load this list. Try again in a moment.";

function Group({
  title,
  description,
  href,
  count,
  flag,
  children,
}: {
  title: string;
  description: string;
  href: string;
  count: number | null;
  flag?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-5 sm:p-6 min-w-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base sm:text-lg font-semibold text-fg">{title}</h2>
          <p className="mt-1 text-sm text-fg-muted leading-relaxed">{description}</p>
        </div>
        {flag && (
          <span className="shrink-0 rounded-full bg-amber-500/15 px-2.5 py-0.5 text-xs font-semibold text-amber-500">
            {flag}
          </span>
        )}
      </div>
      <div className="mt-3">{children}</div>
      {count !== null && count > 0 && (
        <Link
          href={href}
          className="mt-2 inline-flex h-10 items-center gap-1 rounded-xl px-2 -ml-2 text-sm font-semibold text-accent hover:bg-surface-overlay transition-colors"
        >
          {count > PREVIEW_ROWS ? `All ${count}` : "Open"}
          <Icon name="chevron-right" size="w-4 h-4" />
        </Link>
      )}
    </section>
  );
}

function Row({ title, detail }: { title: string; detail?: string }) {
  return (
    <li className="py-2.5 border-t border-line first:border-t-0 min-w-0">
      <div className="text-[15px] font-semibold text-fg break-words line-clamp-2" title={title}>
        {title}
      </div>
      {detail && <div className="text-sm text-fg-muted mt-0.5 break-words">{detail}</div>}
    </li>
  );
}

function Rows({ loading, empty, children }: { loading: boolean; empty: string; children: ReactNode[] }) {
  if (loading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-5 w-3/4" />
        <Skeleton className="h-5 w-1/2" />
      </div>
    );
  }
  if (children.length === 0) return <p className="text-[15px] text-fg-muted">{empty}</p>;
  return <ul>{children}</ul>;
}

function factDetail(f: StandingFact): string {
  const by = f.recorded_by_role === "teammate" ? ` · per ${f.recorded_by_name || "a teammate"}` : "";
  if (f.status === "proposed") return `Waiting for your OK${by}`;
  const kind = f.decision ? (f.outcome ? `Decision · outcome: ${f.outcome}` : "Decision") : f.subject;
  return `${kind} · ${formatDate(f.created_at)}${by}`;
}

export default function KnowsOverview({ pulse, lastVisit }: { pulse: PulseData; lastVisit: string | null }) {
  const { data } = pulse;
  const [facts, setFacts] = useState<{ rows: StandingFact[]; toApprove: number } | null>(null);
  // undefined while loading; null when this viewer keeps none (the link goes).
  const [figures, setFigures] = useState<WorkedOutFigure[] | null | undefined>(undefined);
  const [notes, setNotes] = useState<number | null | undefined>(undefined);
  const [misses, setMisses] = useState<number | null | undefined>(undefined);
  // A list that couldn't be read says so, rather than looking empty.
  const [failed, setFailed] = useState<{ facts?: boolean; figures?: boolean }>({});

  useEffect(() => {
    let cancelled = false;
    listStandingFacts()
      .then((page) => {
        if (cancelled) return;
        const proposed = page.can_review ? page.facts.filter((f) => f.status === "proposed") : [];
        const active = page.facts.filter((f) => f.status === "active" && f.kind !== "profile");
        setFacts({ rows: [...proposed, ...active], toApprove: proposed.length });
      })
      .catch(() => {
        if (cancelled) return;
        setFailed((f) => ({ ...f, facts: true }));
        setFacts({ rows: [], toApprove: 0 });
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
    getHistory()
      .then((state) => {
        if (!cancelled) setNotes(state ? state.notes.length : null);
      })
      .catch(() => {
        if (!cancelled) setNotes(null);
      });
    getReflection()
      .then((state) => {
        if (!cancelled) setMisses(state ? state.misses.length : null);
      })
      .catch(() => {
        if (!cancelled) setMisses(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const projects = data ? [...data.initiatives].sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? "")) : null;
  const olderAdvice = data ? data.advice.length : 0;

  return (
    <div className="space-y-4">
      <NewSinceCard since={lastVisit} />
      <div className="space-y-2">
        <AboutYouCard title="About you" description="Learned from talking with you. Remove any line that's wrong. Only you see this." />
        {notes !== null && notes !== undefined && (
          <LinkLine href="/memories?tab=history" label={`Your notes (${notes})`} detail="What you said, in chat and in replies you approved. Only you see these." />
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Group
          title="About the company"
          description="The projects it's tracking, from planned to completed."
          href="/memories?tab=initiatives"
          count={projects ? projects.length : null}
        >
          <Rows loading={projects === null} empty="No projects yet. They're picked up from your chats.">
            {(projects ?? []).slice(0, PREVIEW_ROWS).map((p) => (
              <Row key={p.id} title={p.title} detail={`${p.status.charAt(0).toUpperCase()}${p.status.slice(1)} · ${formatDate(p.updated_at ?? p.created_at)}`} />
            ))}
          </Rows>
        </Group>

        <Group
          title="What you taught it"
          description="Facts, corrections and decisions you asked it to keep. Used in every answer."
          href="/memories?tab=corrections"
          count={facts ? facts.rows.length : null}
          flag={facts && facts.toApprove > 0 ? `${facts.toApprove} to approve` : undefined}
        >
          <Rows
            loading={facts === null}
            empty={failed.facts ? COULD_NOT_LOAD : "Nothing yet. Tell it a fact or a correction in chat and it keeps it."}
          >
            {(facts?.rows ?? []).slice(0, PREVIEW_ROWS).map((f) => (
              <Row key={f.id} title={f.statement} detail={factDetail(f)} />
            ))}
          </Rows>
        </Group>

        {figures !== null && (
          <Group
            title="What it worked out"
            description="Figures from its own work, with where they came from. Only you see these."
            href="/memories?tab=worked_out"
            count={figures === undefined ? null : figures.length}
          >
            <Rows
              loading={figures === undefined}
              empty={failed.figures ? COULD_NOT_LOAD : "Nothing yet. Figures it works out from your documents show up here."}
            >
              {(figures ?? []).slice(0, PREVIEW_ROWS).map((f) => (
                <Row key={f.id} title={`${f.subject}: ${f.figure}`} detail={figureSourceLine(f)} />
              ))}
            </Rows>
          </Group>
        )}

        {misses !== null && (
          <Group
            title="Learning"
            description="What fell short in your chats, and what it learned from it."
            href="/memories?tab=reflection"
            count={misses === undefined ? null : Math.max(misses, 1)}
          >
            <p className="text-[15px] text-fg-muted">
              {misses === undefined ? "Loading…" : misses === 0 ? "Nothing fell short lately." : `${misses} answers fell short and were looked at again.`}
            </p>
          </Group>
        )}
      </div>

      {olderAdvice > 0 && (
        <LinkLine href="/memories?tab=advice" label={`Older advice (${olderAdvice})`} detail="Advice from before it kept figures instead." />
      )}
    </div>
  );
}

function LinkLine({ href, label, detail }: { href: string; label: string; detail: string }) {
  return (
    <Link
      href={href}
      className="flex items-center justify-between gap-3 rounded-2xl border border-line bg-surface-elevated px-5 py-3.5 hover:border-accent/50 hover:bg-surface-overlay transition-colors min-w-0"
    >
      <div className="min-w-0">
        <div className="text-[15px] font-semibold text-fg">{label}</div>
        <div className="text-sm text-fg-muted break-words">{detail}</div>
      </div>
      <Icon name="chevron-right" size="w-4 h-4" />
    </Link>
  );
}
