"use client";

// Edit on a held step's card (an email's subject and text, a message):
// change the card's own fields, then Approve sends the changed ones as
// ``edits.input``. Who it goes to never changes here.

import { useState } from "react";

import Button from "@/components/ui/Button";
import type { LeadCardField } from "@/lib/api";

export default function HeldFieldsEdit({
  id,
  fields,
  busy,
  onApprove,
  onCancel,
}: {
  id: number;
  fields: LeadCardField[];
  busy: boolean;
  onApprove: (changed: Record<string, string>) => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(
    () => Object.fromEntries(fields.map((f) => [f.field, f.value])),
  );
  const changed = Object.fromEntries(
    fields.filter((f) => (values[f.field] ?? "") !== f.value).map((f) => [f.field, values[f.field] ?? ""]),
  );
  const common =
    "w-full rounded-xl border border-line bg-surface p-3 text-[15px] leading-snug text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50";

  return (
    <div className="mt-3 flex flex-col gap-3">
      {fields.map((f) => {
        const inputId = `held-${id}-${f.field}`;
        return (
          <label key={f.field} htmlFor={inputId} className="flex flex-col gap-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{f.label}</span>
            {f.long ? (
              <textarea
                id={inputId}
                rows={5}
                value={values[f.field] ?? ""}
                disabled={busy}
                onChange={(e) => setValues({ ...values, [f.field]: e.target.value })}
                className={`${common} whitespace-pre-wrap`}
              />
            ) : (
              <input
                id={inputId}
                value={values[f.field] ?? ""}
                disabled={busy}
                onChange={(e) => setValues({ ...values, [f.field]: e.target.value })}
                className={common}
              />
            )}
          </label>
        );
      })}
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button variant="primary" disabled={busy} onClick={() => onApprove(changed)}>
          Approve
        </Button>
      </div>
    </div>
  );
}

// The editable fields a held step's payload carries (department_gate.hold).
export function heldFields(raw: unknown): LeadCardField[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((f) => f && typeof f.field === "string" && typeof f.value === "string")
    .map((f) => ({ field: f.field, label: String(f.label ?? f.field), long: Boolean(f.long), value: f.value }));
}
