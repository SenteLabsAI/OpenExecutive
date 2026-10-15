"use client";

import Link from "next/link";
import { useEffect, useId, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import SectionTabs, { sectionPanelProps } from "@/components/ui/SectionTabs";
import { getWhatItMayDo, type MayDoOutcome, type MayDoRow, type MayDoSection, type WhatItMayDo } from "@/lib/api";
import { SENDS_HREF, WRITES_HREF } from "@/lib/reach";

// Delegate → What it may do: read-only. One row per thing that can go out
// without asking, what happens to it, and the setting that decides it, with
// a link there. The three parts (as you, as the Executive, departments) sit
// behind pills so the page stays one screen. Everything is worked out on the
// server from the settings the gates themselves read (delegation/may_do.py).

const TAB_LABELS: Record<MayDoSection["key"], string> = {
  as_you: "As you",
  executive: "As the Executive",
  departments: "Departments",
};

const OUTCOME_DOT: Record<MayDoOutcome, string> = {
  goes: "bg-emerald-500",
  waits: "bg-amber-500",
  always_waits: "bg-red-500",
  drafts: "bg-fg-subtle",
  off: "bg-fg-subtle/60",
};

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

function Outcome({ outcome, then }: { outcome: MayDoOutcome; then: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm font-medium text-fg whitespace-nowrap">
      <span aria-hidden className={`h-2 w-2 flex-shrink-0 rounded-full ${OUTCOME_DOT[outcome]}`} />
      {then}
    </span>
  );
}

function Row({ row }: { row: MayDoRow }) {
  const link = settingHref(row.setting);
  return (
    <li className="grid gap-1.5 px-4 py-3.5 sm:grid-cols-[minmax(0,1.1fr)_11rem_minmax(0,1.3fr)] sm:gap-4">
      <div className="min-w-0">
        <p className="text-[15px] text-fg leading-snug">{row.what}</p>
        {row.items.length > 0 && (
          <ul className="mt-1.5 flex flex-wrap gap-1.5">
            {row.items.map((item) => (
              <li key={item} className="rounded-md border border-line bg-surface-overlay px-2 py-0.5 text-xs text-fg-muted">
                {item}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <Outcome outcome={row.outcome} then={row.then} />
      </div>
      <p className="min-w-0 text-sm text-fg-muted leading-relaxed">
        {row.why}
        {link && (
          <>
            {" "}
            <Link href={link.href} className="font-medium text-accent underline-offset-2 hover:underline">
              {link.label}
            </Link>
          </>
        )}
      </p>
    </li>
  );
}

function Section({ section }: { section: MayDoSection }) {
  if (!section.available) {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted leading-relaxed">{section.unavailable}</p>
      </SettingsCard>
    );
  }
  return (
    <div className="space-y-3">
      {section.notes.map((note) => (
        <p
          key={note}
          className="rounded-r-xl border-l-[3px] border-amber-500 bg-amber-500/10 px-4 py-2.5 text-sm text-fg leading-relaxed"
        >
          {note}
        </p>
      ))}
      <section className="overflow-hidden rounded-2xl border border-line bg-surface-elevated">
        <h2 className="border-b border-line px-4 py-3 text-base font-semibold text-fg">{section.title}</h2>
        <div
          aria-hidden
          className="hidden border-b border-line bg-surface-overlay px-4 py-2 text-xs font-medium uppercase tracking-wider text-fg-subtle sm:grid sm:grid-cols-[minmax(0,1.1fr)_11rem_minmax(0,1.3fr)] sm:gap-4"
        >
          <span>What</span>
          <span>Then</span>
          <span>Why</span>
        </div>
        {section.rows.length === 0 ? (
          <p className="px-4 py-4 text-sm text-fg-muted">There&apos;s nothing here yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {section.rows.map((row) => (
              <Row key={row.id} row={row} />
            ))}
          </ul>
        )}
      </section>
      {section.footer && <p className="px-1 text-sm text-fg-subtle leading-relaxed">{section.footer}</p>}
    </div>
  );
}

export default function WhatItMayDoView() {
  const [data, setData] = useState<WhatItMayDo | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "hidden" | "error">("loading");
  const [active, setActive] = useState<MayDoSection["key"]>("as_you");
  const idBase = useId();

  useEffect(() => {
    const controller = new AbortController();
    getWhatItMayDo(controller.signal)
      .then((found) => {
        setData(found);
        setState(found ? "ready" : "hidden");
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        console.warn("What it may do:", err);
        setState("error");
      });
    return () => controller.abort();
  }, []);

  if (state === "loading") return <p className="text-sm text-fg-muted">Loading…</p>;
  if (state === "error") {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted">Couldn&apos;t load this. Reload the page to try again.</p>
      </SettingsCard>
    );
  }
  if (state === "hidden" || !data) {
    return (
      <SettingsCard>
        <p className="text-[15px] text-fg-muted">Only people on the team can see this.</p>
      </SettingsCard>
    );
  }

  // The Executive's part is the owner's alone: no tab for anyone else.
  const sections = data.sections.filter((s) => s.available || s.key !== "executive");
  const current = sections.find((s) => s.key === active) ?? sections[0];

  return (
    <div className="space-y-5">
      {data.paused && (
        <p className="rounded-xl border border-line bg-surface-overlay px-4 py-3 text-sm text-fg leading-relaxed">
          The Executive is paused, so nothing below runs on its own until it resumes. What you send or approve
          yourself still goes.{" "}
          <Link href="/settings/executive" className="font-medium text-accent underline-offset-2 hover:underline">
            Resume
          </Link>
        </p>
      )}
      <SectionTabs
        label="What it may do"
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
        <Section section={current} />
      </div>
      <p className="px-1 text-xs text-fg-subtle">
        The number on each pill is how many things go out without asking. Nothing on this page changes a setting.
      </p>
    </div>
  );
}
