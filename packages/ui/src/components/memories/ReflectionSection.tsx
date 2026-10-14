"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import Icon from "@/components/Icon";
import Switch from "@/components/Switch";
import Button from "@/components/ui/Button";
import {
  approveFinding,
  dismissFinding,
  getLookBack,
  retireLesson,
  updateLesson,
  updateLookBackSettings,
  type ExecutiveLesson,
  type LookBack,
  type LookBackFinding,
  type LookBackRun,
} from "@/lib/api";
import { Skeleton } from "./shared";

// Reflection, looking back: what last night's review of the owner's chats
// found, the lessons they approved, and their switches. A card on the Pulse
// overview links to the full screen (`/memories?tab=reflection`). Nothing
// here is pushed anywhere else: the owner opens it when they choose. The
// endpoint is the owner's alone, so for anyone else (a 403) the card and the
// screen say so instead of rendering an empty state.

const KIND_LABEL: Record<LookBackFinding["kind"], string> = {
  fact: "Fact",
  lesson: "Lesson",
  gap: "Knowledge gap",
  product: "Product issue",
};

const KIND_PILL: Record<LookBackFinding["kind"], string> = {
  fact: "bg-surface-overlay text-fg-muted",
  lesson: "bg-indigo-500/15 text-indigo-500",
  gap: "bg-amber-500/15 text-amber-500",
  product: "bg-rose-500/15 text-rose-500",
};

const STATUS_LABEL: Record<LookBackFinding["status"], string> = {
  proposed: "Waiting for you",
  approved: "Approved",
  dismissed: "Dismissed",
  filed: "Not taken up",
  recorded: "In Corrections",
};

function dayLabel(iso: string): string {
  const d = new Date(`${iso}T12:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" });
}

function useLookBack() {
  // undefined while loading; null when this viewer isn't the owner.
  const [data, setData] = useState<LookBack | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    getLookBack()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, error, reload, setData };
}

/** The overview card: one line about the last look back, linking to the screen. */
export function ReflectionCard() {
  const { data } = useLookBack();
  if (data === null) return null;
  let line = "";
  if (data) {
    const waiting = data.week.waiting;
    if (!data.settings.enabled) line = "Looking back at its chats is off. Turn it on in Settings → Your Executive.";
    else if (waiting > 0) line = `${waiting} proposal${waiting === 1 ? "" : "s"} waiting for you from looking back at your chats.`;
    else if (data.week.conversations_reviewed > 0)
      line = `Looked back at ${data.week.conversations_reviewed} conversation${
        data.week.conversations_reviewed === 1 ? "" : "s"
      } this week. Nothing waiting for you.`;
    else line = "It looks back at yesterday's conversations each morning. Nothing reviewed yet.";
  }
  return (
    <Link
      href="/memories?tab=reflection"
      className="group flex items-center justify-between gap-4 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-6 hover:border-accent/50 hover:bg-surface-overlay transition-colors"
    >
      <div className="min-w-0">
        <h2 className="text-lg font-semibold text-fg">Reflection</h2>
        {data === undefined ? <Skeleton className="h-5 w-64 mt-2" /> : <p className="text-[15px] text-fg-muted mt-1">{line}</p>}
      </div>
      <Icon name="chevron-right" size="w-5 h-5" />
    </Link>
  );
}

/** The full screen: this week, the runs with their findings, the lessons. */
export function ReflectionScreen() {
  const { data, error, reload } = useLookBack();
  if (data === null) {
    return <p className="text-[15px] text-fg-muted">Only the owner can see what the Executive learned from its chats.</p>;
  }
  if (error) return <p className="text-[15px] text-rose-500">{error}</p>;
  if (data === undefined) return <Skeleton className="h-40 w-full" />;

  const active = data.lessons.filter((l) => l.status === "active");
  return (
    <div className="space-y-8">
      <WeekRow data={data} />
      {!data.settings.enabled && (
        <p className="rounded-xl border border-amber-400/50 bg-amber-400/10 px-4 py-3 text-[15px] text-fg">
          Looking back is off, so nothing new is reviewed. Turn it on in{" "}
          <Link href="/settings/executive" className="font-medium text-accent underline-offset-2 hover:underline">
            Settings → Your Executive
          </Link>
          .
        </p>
      )}
      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-fg">Looking back</h2>
        {data.runs.length === 0 ? (
          <p className="text-[15px] text-fg-muted">
            Nothing yet. The first look back runs with tomorrow&apos;s reflection, before your morning brief.
          </p>
        ) : (
          data.runs.map((run) => <RunCard key={run.id} run={run} onChange={reload} />)
        )}
      </section>
      <section className="space-y-3">
        <h2 className="text-lg font-semibold text-fg">What it has learned</h2>
        {active.length === 0 ? (
          <p className="text-[15px] text-fg-muted">No lessons yet. Approve one above and it applies from the next conversation.</p>
        ) : (
          <ul className="rounded-2xl border border-line divide-y divide-line">
            {active.map((lesson) => (
              <LessonRow key={lesson.id} lesson={lesson} onChange={reload} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function WeekRow({ data }: { data: LookBack }) {
  const stats: { n: number | string; label: string }[] = [
    { n: data.week.conversations_reviewed, label: "conversations reviewed this week" },
    { n: data.week.findings, label: "findings" },
    { n: data.week.lessons_active, label: "lessons active" },
    { n: data.week.waiting, label: "waiting for you" },
  ];
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {stats.map((s) => (
        <div key={s.label} className="rounded-2xl border border-line bg-surface p-4">
          <div className="text-2xl font-bold tabular-nums text-fg leading-tight">{s.n}</div>
          <div className="mt-1 text-sm text-fg-muted">{s.label}</div>
        </div>
      ))}
    </div>
  );
}

function RunCard({ run, onChange }: { run: LookBackRun; onChange: () => void }) {
  const [open, setOpen] = useState(run.findings.some((f) => f.status === "proposed"));
  const summary =
    run.status === "failed"
      ? "did not finish"
      : run.findings.length === 0
        ? `${run.sessions_reviewed} conversation${run.sessions_reviewed === 1 ? "" : "s"} · nothing to change`
        : `${run.sessions_reviewed} conversation${run.sessions_reviewed === 1 ? "" : "s"} · ${run.findings.length} finding${
            run.findings.length === 1 ? "" : "s"
          }`;
  if (run.findings.length === 0) {
    return (
      <div className="flex items-center justify-between gap-4 rounded-2xl border border-line bg-surface px-4 py-3">
        <span className="text-[15px] font-medium text-fg">{dayLabel(run.day)}</span>
        <span className="text-sm text-fg-muted">{summary}</span>
      </div>
    );
  }
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5 space-y-4">
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center justify-between gap-4 text-left">
        <span className="text-base font-semibold text-fg">Looking back at {dayLabel(run.day)}</span>
        <span className="text-sm text-fg-muted">{summary}</span>
      </button>
      {open &&
        run.findings.map((f) => (
          <FindingCard key={f.id} finding={f} onChange={onChange} />
        ))}
    </section>
  );
}

function FindingCard({ finding, onChange }: { finding: LookBackFinding; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(finding.proposal);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      setEditing(false);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const waiting = finding.status === "proposed";
  return (
    <div className="rounded-xl border border-line p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${KIND_PILL[finding.kind]}`}>{KIND_LABEL[finding.kind]}</span>
        {!waiting && <span className="rounded-full bg-surface-overlay px-2.5 py-0.5 text-xs font-semibold text-fg-muted">{STATUS_LABEL[finding.status]}</span>}
        {finding.resolution_note === "edited" && <span className="text-xs text-fg-muted">with edits</span>}
      </div>
      <h3 className="text-[15px] font-semibold text-fg">{finding.title}</h3>
      <p className="text-sm text-fg-muted">{finding.what_happened}</p>
      {finding.evidence.length > 0 && (
        <ul className="border-l-2 border-line pl-3 space-y-1">
          {finding.evidence
            .filter((e) => e.quote)
            .slice(0, 3)
            .map((e) => (
              <li key={e.message_id} className="text-sm text-fg-muted">
                &ldquo;{e.quote}&rdquo;
              </li>
            ))}
        </ul>
      )}
      {finding.kind === "lesson" && (
        editing ? (
          <textarea
            id={`lesson-edit-${finding.id}`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={2}
            className="w-full rounded-lg border border-line bg-surface-input px-3 py-2 text-[15px] text-fg"
          />
        ) : (
          <p className="rounded-lg bg-surface-overlay px-3 py-2 text-[15px] text-fg">{finding.proposal}</p>
        )
      )}
      {finding.kind !== "lesson" && <p className="text-[15px] text-fg">{finding.proposal}</p>}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-2">
          {waiting && finding.kind === "lesson" && !editing && (
            <>
              <Button variant="primary" size="sm" disabled={busy} onClick={() => run(() => approveFinding(finding.id))}>
                Approve
              </Button>
              <Button size="sm" disabled={busy} onClick={() => setEditing(true)}>
                Approve with edits
              </Button>
            </>
          )}
          {waiting && finding.kind === "lesson" && editing && (
            <>
              <Button variant="primary" size="sm" disabled={busy} onClick={() => run(() => approveFinding(finding.id, text))}>
                Save and approve
              </Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </>
          )}
          {waiting && finding.kind !== "lesson" && (
            <Button size="sm" disabled={busy} onClick={() => run(() => approveFinding(finding.id))}>
              Noted
            </Button>
          )}
          {waiting && (
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => run(() => dismissFinding(finding.id))}>
              Dismiss
            </Button>
          )}
        </div>
        <Link
          href={`/?session=${encodeURIComponent(finding.session_id)}`}
          className="text-sm font-medium text-accent underline-offset-2 hover:underline"
        >
          See the conversation
        </Link>
      </div>
      {error && <p className="text-sm text-rose-500">{error}</p>}
    </div>
  );
}

function LessonRow({ lesson, onChange }: { lesson: ExecutiveLesson; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(lesson.text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      setEditing(false);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="p-4 space-y-2">
      {editing ? (
        <textarea
          id={`lesson-${lesson.id}`}
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          className="w-full rounded-lg border border-line bg-surface-input px-3 py-2 text-[15px] text-fg"
        />
      ) : (
        <p className="text-[15px] text-fg">{lesson.text}</p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm text-fg-muted">
          Learned {new Date(lesson.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })}
          {lesson.locked ? " · locked, a review never rewrites it" : ""}
        </span>
        <div className="flex flex-wrap gap-2">
          {editing ? (
            <>
              <Button variant="primary" size="sm" disabled={busy} onClick={() => run(() => updateLesson(lesson.id, { text }))}>
                Save
              </Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(true)}>
                Edit
              </Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => run(() => updateLesson(lesson.id, { locked: !lesson.locked }))}>
                {lesson.locked ? "Unlock" : "Lock"}
              </Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => run(() => retireLesson(lesson.id))}>
                Retire
              </Button>
            </>
          )}
        </div>
      </div>
      {error && <p className="text-sm text-rose-500">{error}</p>}
    </li>
  );
}

/** The switch row used by Settings → Your Executive. */
export function LookBackSwitches() {
  const { data, error, setData } = useLookBack();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  if (data === null) return null;
  if (error) return <p className="text-sm text-rose-500">{error}</p>;
  if (data === undefined) return <Skeleton className="h-6 w-full" />;

  async function save(body: { enabled?: boolean; apply_mode?: "ask" | "auto" }) {
    setSaving(true);
    setSaveError(null);
    try {
      const settings = await updateLookBackSettings(body);
      setData((prev) => (prev ? { ...prev, settings } : prev));
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const s = data.settings;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div id="look-back-switch" className="text-[15px] font-semibold text-fg">
            Looks back at its chats
          </div>
          <p className="text-sm text-fg-muted">
            Each morning it reads yesterday&apos;s conversations and writes up what it could have done better. You read it when
            you want to, on{" "}
            <Link href="/memories?tab=reflection" className="font-medium text-accent underline-offset-2 hover:underline">
              Pulse → Reflection
            </Link>
            , and nothing changes until you approve it there. Your chats stay in your workspace. Private mail chats are never read.
          </p>
        </div>
        <Switch checked={s.enabled} disabled={saving} labelledBy="look-back-switch" onChange={(v) => save({ enabled: v })} />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-[15px] text-fg">Changes it proposes</span>
        <div className="inline-flex rounded-lg border border-line overflow-hidden text-sm">
          <button
            type="button"
            disabled={saving}
            onClick={() => save({ apply_mode: "ask" })}
            className={`px-3 py-1.5 ${s.apply_mode === "ask" ? "bg-surface-overlay font-medium text-fg" : "text-fg-muted"}`}
          >
            Ask me first
          </button>
          <button
            type="button"
            disabled={saving || !s.auto_apply_available}
            title={s.auto_apply_available ? undefined : "Opens after ten approvals with no edits"}
            onClick={() => save({ apply_mode: "auto" })}
            className={`px-3 py-1.5 ${s.apply_mode === "auto" ? "bg-surface-overlay font-medium text-fg" : "text-fg-muted"} disabled:opacity-50`}
          >
            Apply on its own
          </button>
        </div>
      </div>
      <p className="text-sm text-fg-muted">
        {s.auto_apply_available
          ? "You've approved enough as written that it can apply lessons on its own. The screen still lists each one."
          : `Apply on its own opens after ten approvals with no edits (${s.clean_approvals} so far). Facts always go to Corrections first.`}
      </p>
      {saveError && <p className="text-sm text-rose-500">{saveError}</p>}
    </div>
  );
}
