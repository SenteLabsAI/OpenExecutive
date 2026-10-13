"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { buttonClass } from "@/components/ui/Button";
import { listDepartments, listPeople, type Person } from "@/lib/api";
import { FIRST_RUN_LINKS } from "@/lib/firstRun";

interface Props {
  /** Personal (one person) or a team; sets which rows are shown. */
  solo: boolean;
  /** Opens Home. */
  onDone: () => void;
}

// The last setup screen: what was saved, then the two connections that make
// the Executive useful, each explained as its own thing. The first is the
// Executive's own account (it reads, sends and keeps a calendar as that
// account, and the morning brief comes from it). The second is the owner's
// own mailbox for Act as me, which is optional. Both are also on the
// getting-started checklist on Home, so skipping here loses nothing.
export default function OnboardDone({ solo, onDone }: Props) {
  const [companyName, setCompanyName] = useState<string | null>(null);
  const [owner, setOwner] = useState<Person | null>(null);
  const [peopleCount, setPeopleCount] = useState<number | null>(null);
  const [departmentCount, setDepartmentCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/backend/health")
      .then((r) => r.json())
      .then((h: { company_name?: string }) => {
        if (!cancelled) setCompanyName(h.company_name?.trim() || null);
      })
      .catch(() => undefined);
    listPeople()
      .then((ps) => {
        if (cancelled) return;
        setOwner(ps.find((p) => p.is_principal) ?? null);
        setPeopleCount(ps.length);
      })
      .catch(() => undefined);
    if (!solo) {
      listDepartments()
        .then((ds) => {
          if (!cancelled) setDepartmentCount(ds.length);
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [solo]);

  const rows: [string, string][] = [];
  if (companyName) rows.push([solo ? "Your work" : "Company", companyName]);
  if (owner) {
    rows.push(["You", [owner.full_name, owner.role].filter(Boolean).join(", ")]);
    if (owner.email) rows.push(["Sign-in", owner.email]);
  }
  if (!solo && peopleCount !== null) {
    const parts = [`${peopleCount} ${peopleCount === 1 ? "person" : "people"}`];
    if (departmentCount !== null) {
      parts.push(`${departmentCount} ${departmentCount === 1 ? "department" : "departments"}`);
    }
    rows.push(["Team", parts.join(", ")]);
  }

  return (
    <div className="max-w-2xl mx-auto px-4 sm:px-6 py-12 sm:py-16 w-full">
      <div className="text-4xl leading-none text-emerald-400" aria-hidden>
        ✓
      </div>
      <h1 className="mt-3 text-2xl font-bold tracking-tight text-fg">Your Executive is ready</h1>
      <p className="text-sm text-fg-muted mt-1">
        Here is what was saved. Everything stays editable in Settings.
      </p>

      {rows.length > 0 && (
        <dl className="mt-6 rounded-2xl border border-line bg-surface-elevated px-4 py-1">
          {rows.map(([k, v]) => (
            <div
              key={k}
              className="flex justify-between gap-4 py-3 text-[15px] border-b border-line last:border-0"
            >
              <dt className="text-fg-muted">{k}</dt>
              <dd className="text-fg text-right">{v}</dd>
            </div>
          ))}
        </dl>
      )}

      <h2 className="mt-8 text-xs font-semibold uppercase tracking-wider text-fg-subtle">
        Two accounts, two jobs
      </h2>

      <section className="mt-3 rounded-2xl border border-line bg-surface-overlay p-4">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-fg text-fg-inverse text-sm font-bold">
            1
          </span>
          <div className="min-w-0">
            <h3 className="text-[15px] font-semibold text-fg">Give your Executive its own email</h3>
            <p className="mt-1 text-sm text-fg-muted leading-snug">
              A Google or Microsoft account just for it, like exec@yourcompany.com. Free Gmail or
              Outlook.com works. It reads, sends and keeps a calendar as that account, and your
              morning brief comes from it.
            </p>
          </div>
        </div>
        <div className="mt-3">
          <Link href={FIRST_RUN_LINKS.execMailbox} className={buttonClass("primary", "md")}>
            Connect its account
          </Link>
        </div>
      </section>

      <section className="mt-3 rounded-2xl border border-line bg-surface-overlay p-4">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-fg text-fg-inverse text-sm font-bold">
            2
          </span>
          <div className="min-w-0">
            <h3 className="text-[15px] font-semibold text-fg">
              Let it read your own mailbox{" "}
              <span className="ml-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-xs font-semibold text-accent">
                Optional
              </span>
            </h3>
            <p className="mt-1 text-sm text-fg-muted leading-snug">
              Your Gmail or Outlook, read only. It drafts replies as you and never sends them.
            </p>
          </div>
        </div>
        <div className="mt-3">
          <Link href={FIRST_RUN_LINKS.ownMailbox} className={buttonClass("secondary", "md")}>
            Connect my mailbox
          </Link>
        </div>
      </section>

      <p className="mt-8">
        <button
          type="button"
          onClick={onDone}
          className="text-sm font-semibold text-accent hover:underline cursor-pointer"
        >
          Skip for now, open Home →
        </button>
      </p>
    </div>
  );
}
