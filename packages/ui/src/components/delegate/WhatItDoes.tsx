"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import {
  DELEGATION_CHANGED,
  getWhatItMayDo,
  type MayDoOutcome,
  type MayDoRow,
  type MayDoSection,
  type WhatItMayDo,
} from "@/lib/api";
import { SENDS_HREF, WRITES_HREF } from "@/lib/reach";

// What it does for you: read-only, beside the Delegate tabs
// (WhatItDoesPanel). With the viewer's settings right now, what goes out on
// its own, what asks first and what it only writes, each with the setting
// that decides it. It reloads when a setting is saved (DELEGATION_CHANGED)
// and marks the rows that changed. Everything is worked out on the server
// from the settings the gates themselves read (delegation/may_do.py).

const TAB_LABELS: Record<MayDoSection["key"], string> = {
  as_you: "As you",
  executive: "Executive",
  departments: "Departments",
};

type Group = "goes" | "asks" | "drafts" | "off";

const GROUP_OF: Record<MayDoOutcome, Group> = {
  goes: "goes",
  waits: "asks",
  always_waits: "asks",
  drafts: "drafts",
  off: "off",
};

const GROUP_ORDER: Group[] = ["goes", "asks", "drafts", "off"];

const GROUP_DOT: Record<Group, string> = {
  goes: "bg-emerald-500",
  asks: "bg-amber-500",
  drafts: "bg-fg-subtle",
  off: "bg-fg-subtle/60",
};

function groupTitle(group: Group, section: MayDoSection["key"]): string {
  if (group === "goes") {
    return section === "as_you" ? "Sends on its own" : section === "departments" ? "Acts on its own" : "Does on its own";
  }
  if (group === "asks") return section === "departments" ? "Waits for an approver" : "Asks first";
  if (group === "drafts") return "Writes for you to send";
  return "Off";
}

// How long a changed row stays marked.
const CHANGED_MS = 10_000;

function rowKey(row: MayDoRow): string {
  return `${row.outcome}|${row.what}|${row.why}`;
}

function settingHref(setting: string): { href: string; label: string } | null {
  if (setting === "act_as_me") return { href: WRITES_HREF, label: "Act as me" };
  if (setting === "handle_it") return { href: SENDS_HREF, label: "Handle it for me" };
  if (setting === "take_the_lead") return { href: "/delegate/take-the-lead", label: "Take the lead" };
  if (setting === "pause") return { href: "/settings/executive", label: "Your Executive" };
  if (setting.startsWith("department:")) {
    const slug = setting.slice("department:".length);
    return { href: `/departments/${encodeURIComponent(slug)}`, label: "Department settings" };
  }
  return null;
}

function Row({ row, changed }: { row: MayDoRow; changed: boolean }) {
  const link = settingHref(row.setting);
  return (
    <li
      className={`rounded-xl border bg-surface-elevated px-3.5 py-3 transition-shadow ${
        changed ? "border-emerald-500 ring-4 ring-emerald-500/15" : "border-line"
      }`}
    >
      {changed && (
        <span className="float-right ml-2 text-[11px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
          Just changed
        </span>
      )}
      <p className="text-sm text-fg leading-snug">{row.what}</p>
      {row.why && <p className="mt-1 text-[13px] text-fg-muted leading-relaxed">{row.why}</p>}
      {row.items.length > 0 && (
        <ul className="mt-1.5 flex flex-wrap gap-1.5">
          {row.items.map((item) => (
            <li key={item} className="rounded-md border border-line bg-surface-overlay px-2 py-0.5 text-xs text-fg-muted">
              {item}
            </li>
          ))}
        </ul>
      )}
      {link && (
        <Link
          href={link.href}
          className="mt-2 inline-block rounded-md border border-accent/40 px-2 py-0.5 text-xs font-medium text-accent hover:bg-accent/10"
        >
          {link.label}
        </Link>
      )}
    </li>
  );
}

function Section({ section, changed }: { section: MayDoSection; changed: Set<string> }) {
  if (!section.available) {
    return <p className="text-sm text-fg-muted leading-relaxed">{section.unavailable}</p>;
  }
  if (section.rows.length === 0) return <p className="text-sm text-fg-muted">There&apos;s nothing here yet.</p>;
  return (
    <div className="space-y-4">
      {section.notes.map((note) => (
        <p
          key={note}
          className="rounded-r-xl border-l-[3px] border-amber-500 bg-amber-500/10 px-3 py-2 text-[13px] text-fg leading-relaxed"
        >
          {note}
        </p>
      ))}
      {GROUP_ORDER.map((group) => {
        const rows = section.rows.filter((r) => GROUP_OF[r.outcome] === group);
        if (rows.length === 0) return null;
        return (
          <section key={group}>
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold text-fg">
              <span aria-hidden className={`h-2 w-2 rounded-full ${GROUP_DOT[group]}`} />
              {groupTitle(group, section.key)}
            </h3>
            <ul className="space-y-1.5">
              {rows.map((row) => (
                <Row key={row.id} row={row} changed={changed.has(row.id)} />
              ))}
            </ul>
          </section>
        );
      })}
      {section.footer && <p className="text-[13px] text-fg-subtle leading-relaxed">{section.footer}</p>}
    </div>
  );
}

export default function WhatItDoes() {
  const [data, setData] = useState<WhatItMayDo | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "hidden" | "error">("loading");
  const [active, setActive] = useState<MayDoSection["key"]>("as_you");
  const [changed, setChanged] = useState<Set<string>>(() => new Set());
  const seen = useRef<Map<string, string> | null>(null);
  const clear = useRef<ReturnType<typeof setTimeout> | null>(null);
  const idBase = useId();

  const load = useCallback((signal?: AbortSignal) => {
    getWhatItMayDo(signal)
      .then((found) => {
        const rows = new Map<string, string>();
        found?.sections.forEach((s) => s.rows.forEach((r) => rows.set(`${s.key}:${r.id}`, rowKey(r))));
        const before = seen.current;
        if (before) {
          const now = new Set<string>();
          found?.sections.forEach((s) =>
            s.rows.forEach((r) => {
              if (before.get(`${s.key}:${r.id}`) !== rowKey(r)) now.add(r.id);
            }),
          );
          if (now.size > 0) {
            setChanged(now);
            if (clear.current) clearTimeout(clear.current);
            clear.current = setTimeout(() => setChanged(new Set()), CHANGED_MS);
          }
        }
        seen.current = rows;
        setData(found);
        setState(found ? "ready" : "hidden");
      })
      .catch((err) => {
        if (signal?.aborted) return;
        console.warn("What it does for you:", err);
        setState("error");
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    const reload = () => load();
    window.addEventListener(DELEGATION_CHANGED, reload);
    return () => {
      controller.abort();
      window.removeEventListener(DELEGATION_CHANGED, reload);
      if (clear.current) clearTimeout(clear.current);
    };
  }, [load]);

  if (state === "loading") return <p className="text-sm text-fg-muted">Loading…</p>;
  if (state === "error") return <p className="text-sm text-fg-muted">Couldn&apos;t load this. Reload the page to try again.</p>;
  if (state === "hidden" || !data) return <p className="text-sm text-fg-muted">Only people on the team can see this.</p>;

  // The Executive's part is the owner's alone: no tab for anyone else.
  const sections = data.sections.filter((s) => s.available || s.key !== "executive");
  const current = sections.find((s) => s.key === active) ?? sections[0];

  return (
    <div className="space-y-4">
      {data.paused && (
        <p className="rounded-xl border border-line bg-surface-overlay px-3 py-2.5 text-[13px] text-fg leading-relaxed">
          The Executive is paused, so nothing here runs on its own until it resumes. What you send or approve yourself
          still goes.{" "}
          <Link href="/settings/executive" className="font-medium text-accent underline-offset-2 hover:underline">
            Resume
          </Link>
        </p>
      )}
      <SectionTabs
        label="What it does for you"
        idBase={idBase}
        active={current.key}
        onChange={setActive}
        tabs={sections.map((s) => ({
          id: s.key,
          label: TAB_LABELS[s.key],
          badge: s.available ? s.goes : null,
        }))}
      />
      <div {...sectionPanelProps(idBase, current.key)}>
        <Section section={current} changed={changed} />
      </div>
    </div>
  );
}
