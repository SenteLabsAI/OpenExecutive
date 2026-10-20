"use client";

// Contacts the Executive keeps up to date (people/contact_keeper.py), on the
// Contacts tab: "Changes I made" with an Undo each (Add / Not now on a
// suggestion), and the "Keep my contacts up to date" switch. The
// principal's alone; the page shows it only to them.

import Link from "next/link";

import Button from "@/components/ui/Button";
import {
  decideContactChange,
  markContactChangesSeen,
  setContactKeeperMode,
  type ContactChange,
  type ContactChanges as ContactChangesData,
  type ContactKeeperMode,
  type ContactNote,
} from "@/lib/api";
import {
  addedByLine,
  cardChanges,
  changeLine,
  changeReason,
  contactActivityLines,
  fieldLabel,
  keeperBlocked,
} from "@/lib/contactChanges";

const TAG_CLS = {
  Added: "bg-emerald-500/15 text-emerald-500",
  Updated: "bg-accent/15 text-accent",
  Suggested: "bg-amber-500/15 text-amber-500",
} as const;

const MODES: { id: ContactKeeperMode; label: string }[] = [
  { id: "on", label: "On" },
  { id: "ask", label: "Ask me first" },
  { id: "off", label: "Off" },
];

function ChangeRow({ change, onDecided }: { change: ContactChange; onDecided: () => void }) {
  const line = changeLine(change);
  const decide = (verb: "undo" | "accept" | "dismiss") => {
    decideContactChange(change.id, verb).then(onDecided, onDecided);
  };
  return (
    <li className="flex items-start gap-3 py-3 border-t border-line first:border-t-0">
      <span className={`mt-0.5 shrink-0 rounded-md px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ${TAG_CLS[line.tag]}`}>
        {line.tag}
      </span>
      <div className="min-w-0 flex-1">
        {change.person_id !== null && change.status === "applied" ? (
          <Link href={`/people/${change.person_id}`} className="text-[15px] font-semibold text-fg hover:underline">
            {line.title}
          </Link>
        ) : (
          <span className="text-[15px] font-semibold text-fg">{line.title}</span>
        )}
        <p className="text-sm text-fg-muted mt-0.5 break-words">{line.detail}</p>
      </div>
      {change.status === "suggested" ? (
        <div className="flex shrink-0 flex-col items-end gap-1 sm:flex-row sm:items-center">
          <Button size="sm" variant="secondary" onClick={() => decide("accept")}>Add</Button>
          <Button size="sm" variant="ghost" onClick={() => decide("dismiss")}>Not now</Button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => decide("undo")}
          className="shrink-0 text-sm font-semibold text-accent hover:underline"
        >
          Undo
        </button>
      )}
    </li>
  );
}

export function ContactChangesCard({
  data,
  onChanged,
}: {
  data: ContactChangesData;
  onChanged: (next?: ContactChangesData) => void;
}) {
  const rows = cardChanges(data.changes);
  if (rows.length === 0) return null;
  const onlySuggestions = rows.every((c) => c.status === "suggested");
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-5 mb-4" aria-label="Changes I made">
      <div className="flex items-center justify-between gap-3 mb-1">
        <h2 className="text-[17px] font-semibold text-fg">{onlySuggestions ? "People to add" : "Changes I made"}</h2>
        {!onlySuggestions && (
          <button
            type="button"
            onClick={() => markContactChangesSeen().then(onChanged, () => onChanged())}
            className="text-sm font-semibold text-accent hover:underline"
          >
            Got it
          </button>
        )}
      </div>
      <ul>
        {rows.map((c) => (
          <ChangeRow key={c.id} change={c} onDecided={() => onChanged()} />
        ))}
      </ul>
    </section>
  );
}

export function ContactKeeperSwitch({
  data,
  onChanged,
}: {
  data: ContactChangesData;
  onChanged: (next?: ContactChangesData) => void;
}) {
  const waiting = data.mode !== "off" ? keeperBlocked(data.status) : null;
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-5 mb-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold text-fg">Keep my contacts up to date</h2>
          <p className="text-sm text-fg-muted mt-0.5">
            I add people you email or meet, and fill in names and roles from their own emails.
          </p>
          {waiting && <p className="text-sm text-amber-500 mt-1">{waiting}</p>}
        </div>
        <div role="radiogroup" aria-label="Keep my contacts up to date" className="flex shrink-0 self-start rounded-xl border border-line overflow-hidden">
          {MODES.map((m) => (
            <button
              key={m.id}
              type="button"
              role="radio"
              aria-checked={data.mode === m.id}
              onClick={() => data.mode !== m.id && setContactKeeperMode(m.id).then(onChanged, () => onChanged())}
              className={`px-3 py-2 text-sm ${data.mode === m.id ? "bg-accent/15 text-fg font-semibold" : "text-fg-muted hover:text-fg"}`}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}

function shortDate(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? "" : when.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** On a contact's own page: how they were added and what was filled in,
 * each with an Undo. Nothing when the Executive changed nothing. */
export function PersonContactChanges({
  changes,
  onChanged,
}: {
  changes: ContactChange[];
  onChanged: () => void;
}) {
  const standing = changes.filter((c) => c.status === "applied");
  const added = addedByLine(standing, shortDate);
  const add = standing.find((c) => c.action === "add" && c.reason !== "asked");
  const updates = standing.filter((c) => c.action === "update");
  if (!added && updates.length === 0) return null;
  const undo = (id: number) => decideContactChange(id, "undo").then(onChanged, onChanged);
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated px-5 py-1 mb-4" aria-label="Changes I made">
      {added && add && (
        <p className="py-3 text-sm text-fg-muted">
          <span className="font-semibold text-fg">{added.split(". ")[0]}.</span> {added.split(". ").slice(1).join(". ")}{" "}
          <button type="button" onClick={() => undo(add.id)} className="font-semibold text-accent hover:underline">
            Undo
          </button>
        </p>
      )}
      {updates.map((c) => (
        <div key={c.id} className="flex items-start justify-between gap-3 py-3 border-t border-line first:border-t-0">
          <div className="min-w-0">
            <div className="text-[12px] font-bold uppercase tracking-wide text-fg-subtle">
              {fieldLabel(c.field)}
            </div>
            <div className="text-[15px] text-fg mt-0.5 break-words">{c.new_value}</div>
            <div className="text-[13px] text-fg-subtle mt-0.5">
              {c.old_value ? `Was "${c.old_value}". ` : ""}
              {changeReason(c)}
            </div>
          </div>
          <button type="button" onClick={() => undo(c.id)} className="shrink-0 text-sm font-semibold text-accent hover:underline">
            Undo
          </button>
        </div>
      ))}
    </section>
  );
}

/** On a person's own page (contact or teammate, for the principal only):
 * what the Executive remembers about them, from
 * your own mail and meetings. Nothing when it knows nothing yet. */
export function PersonContactNote({ note }: { note: ContactNote | null }) {
  if (!note) return null;
  const lines = contactActivityLines(note, shortDate);
  if (!note.note && note.topics.length === 0 && lines.length === 0) return null;
  return (
    <section className="rounded-2xl border border-line bg-surface-elevated px-5 py-4 mb-4" aria-label="What I remember">
      <div className="text-[12px] font-bold uppercase tracking-wide text-fg-subtle">What I remember</div>
      {note.note && <p className="text-[15px] text-fg mt-1.5 break-words">{note.note}</p>}
      {note.topics.length > 0 && (
        <ul className="flex flex-wrap gap-1.5 mt-2.5" aria-label="Good to ask about">
          {note.topics.map((t) => (
            <li key={t} className="rounded-full border border-line px-2.5 py-0.5 text-[13px] text-fg-muted">
              {t}
            </li>
          ))}
        </ul>
      )}
      {lines.length > 0 && (
        <ul className="mt-2.5 space-y-0.5 text-[13px] text-fg-subtle">
          {lines.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      )}
      <p className="mt-2.5 text-[12px] text-fg-subtle">From your own emails and meetings. Only you see this.</p>
    </section>
  );
}
