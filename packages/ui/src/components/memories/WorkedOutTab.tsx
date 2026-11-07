"use client";

import { useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import { editWorkedOut, forgetWorkedOut, listWorkedOut, type WorkedOutFigure } from "@/lib/api";
import { EmptyState, formatDate } from "./shared";
import { PageSpinner } from "@/components/ui/Spinner";

// Memory → What it worked out: the figures the Executive worked out for the
// signed-in person (memory.worked_out), each with where it came from and
// when. Theirs alone; each can be corrected or removed. Removing asks on the
// row itself, not in a browser dialog.

const NOTHING_YET =
  "Nothing worked out yet. When it reads a document and answers with a figure, it keeps the figure here with where it came from.";

/** "From the DCA statement · 9 Oct" */
export function figureSourceLine(f: WorkedOutFigure): string {
  const when = formatDate(f.as_of || f.created_at);
  return f.source ? `From ${f.source} · ${when}` : when;
}

export default function WorkedOutTab() {
  // undefined while loading; null when this viewer keeps none.
  const [figures, setFigures] = useState<WorkedOutFigure[] | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listWorkedOut()
      .then((rows) => {
        if (!cancelled) setFigures(rows);
      })
      .catch(() => {
        if (cancelled) return;
        setFailed(true);
        setFigures([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (figures === undefined) return <PageSpinner />;
  if (failed && (figures === null || figures.length === 0)) {
    return <EmptyState message="Couldn't load what it worked out. Try again in a moment." />;
  }
  if (figures === null) {
    return <EmptyState message="Figures are kept for people on the People list, once they're signed in." />;
  }
  if (figures.length === 0) return <EmptyState message={NOTHING_YET} />;

  return (
    <div className="divide-y divide-line">
      {figures.map((f) => (
        <FigureRow
          key={f.id}
          figure={f}
          onChanged={(next) => setFigures((rows) => (rows ?? []).map((r) => (r.id === next.id ? next : r)))}
          onRemoved={() => setFigures((rows) => (rows ?? []).filter((r) => r.id !== f.id))}
        />
      ))}
    </div>
  );
}

function FigureRow({
  figure,
  onChanged,
  onRemoved,
}: {
  figure: WorkedOutFigure;
  onChanged: (f: WorkedOutFigure) => void;
  onRemoved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [subject, setSubject] = useState(figure.subject);
  const [value, setValue] = useState(figure.figure);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await editWorkedOut(figure.id, { subject: subject.trim(), figure: value.trim() }));
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save that change.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await forgetWorkedOut(figure.id);
      onRemoved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove it.");
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <div className="py-3.5 space-y-2">
        <input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          maxLength={120}
          aria-label="What the figure is"
          className="w-full rounded-md border border-line bg-surface px-3 h-10 text-[15px] text-fg"
        />
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          maxLength={200}
          aria-label="The figure"
          className="w-full rounded-md border border-line bg-surface px-3 h-10 text-[15px] text-fg"
        />
        {error && <p className="text-sm text-fg-muted">{error}</p>}
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            className="!h-10"
            disabled={
              busy || !subject.trim() || !value.trim() ||
              (subject.trim() === figure.subject && value.trim() === figure.figure)
            }
            onClick={() => void save()}
          >
            {busy ? "Saving…" : "Save"}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            className="!h-10"
            disabled={busy}
            onClick={() => {
              setSubject(figure.subject);
              setValue(figure.figure);
              setError(null);
              setEditing(false);
            }}
          >
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="py-3.5 flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-[15px] font-semibold text-fg break-words">
          {figure.subject}: {figure.figure}
        </div>
        <div className="text-sm text-fg-muted mt-0.5 break-words">{figureSourceLine(figure)}</div>
        {error && <p className="text-sm text-fg-muted mt-1">{error}</p>}
      </div>
      <div className="shrink-0 flex gap-2">
        {confirming ? (
          <>
            <Button variant="danger" size="sm" className="!h-10" disabled={busy} onClick={() => void remove()}>
              {busy ? "Removing…" : "Remove it"}
            </Button>
            <Button variant="secondary" size="sm" className="!h-10" disabled={busy} onClick={() => setConfirming(false)}>
              Keep
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" size="sm" className="!h-10" onClick={() => setEditing(true)}>
              Edit
            </Button>
            <Button variant="secondary" size="sm" className="!h-10" onClick={() => setConfirming(true)}>
              Remove
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
