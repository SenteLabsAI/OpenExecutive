"use client";

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { WhatItDoesAside, WhatItDoesSheet, useIsWide, useWhatItDoesOpen } from "@/components/delegate/WhatItDoesPanel";
import { useDelegateTab } from "@/components/delegate/useDelegateTab";
import { useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";
import ActAsMeCard, { TeamSection } from "@/components/settings/ActAsMeCard";
import AsMeCard, { useTakeTheLead } from "@/components/settings/AsMeCard";
import BeforeAnythingCard from "@/components/settings/BeforeAnythingCard";
import SentCard from "@/components/settings/SentCard";
import SettingsCard from "@/components/settings/SettingsCard";
import { useDelegation } from "@/components/settings/useDelegation";
import { getOutsideContent, type OutsideContentSetting } from "@/lib/api";
import { approvalsTitle, onApprovalsChanged } from "@/lib/approvalsLevel";
import {
  APPROVALS_ANCHOR,
  AS_ME_ANCHOR,
  AS_YOU_ANCHOR,
  DELEGATE_TABS,
  asYouBadge,
  type DelegateTab,
} from "@/lib/reach";
import { PageSpinner } from "@/components/ui/Spinner";

// Delegate: one page, three tabs. As you (Act as me): what it writes and
// sends in your name, from your own mailbox, and how you write. As me (Take
// the lead): what it does in its own name without being asked. Approvals:
// the one level for both, and your rules. In
// training, on either side, holds everything for you first. Each tab shows
// where it stands (Your inbox, with In training while it is on; On; Balanced), so all three read at a glance.
// What it does for you sits beside the page (to the right on a wide screen,
// a sheet on a narrow one).

const TAB_NAME: Record<DelegateTab, string> = {
  [AS_YOU_ANCHOR]: "As you",
  [AS_ME_ANCHOR]: "As me",
  [APPROVALS_ANCHOR]: "Approvals",
};

export default function DelegatePage() {
  const delegation = useDelegation();
  const lead = useTakeTheLead();
  const [approvals, setApprovals] = useState<OutsideContentSetting | null>(null);
  const [open, setOpen] = useWhatItDoesOpen();
  const wide = useIsWide();
  const [sheet, setSheet] = useState(false);
  const closeSheet = useCallback(() => setSheet(false), []);
  const [tab, pickTab] = useDelegateTab();
  const { status, unknown } = useExecutiveStatus();

  useEffect(() => {
    const controller = new AbortController();
    getOutsideContent(controller.signal)
      .then(setApprovals)
      .catch(() => setApprovals(null));
    // The Approvals card and the sidebar's quick picker save the same setting.
    const stop = onApprovalsChanged<OutsideContentSetting>(setApprovals);
    return () => {
      controller.abort();
      stop();
    };
  }, []);

  const level = approvals?.mode ?? null;
  const settings = delegation.state === "ready" ? delegation.settings : null;
  const button =
    "flex-none rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface-overlay";

  return (
    <div className="flex flex-1 min-h-0">
      <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 pt-6 sm:pt-8 pb-16">
          <div className="flex items-start justify-between gap-3">
            <h1 className="text-2xl sm:text-3xl tracking-tight font-semibold">Delegate</h1>
            <button type="button" onClick={() => setSheet(true)} className={`${button} mt-1 lg:hidden`}>
              What it does for you
            </button>
            {!open && (
              <button type="button" onClick={() => setOpen(true)} className={`${button} mt-1 hidden lg:block`}>
                What it does for you
              </button>
            )}
          </div>
          <p className="mt-3 text-[15px] text-fg-muted leading-relaxed">
            The Executive can work for you two ways. <span className="font-semibold text-fg">As you</span>: in your
            name, from your accounts, so it looks like you did it.{" "}
            <span className="font-semibold text-fg">As me</span>: in its own name, from its own accounts, so people
            see your Executive did it. Both learn from you. One Approvals setting decides what waits for your yes.
          </p>

          <DelegateTabs
            current={tab}
            onPick={pickTab}
            badges={{
              [AS_YOU_ANCHOR]: settings ? { text: asYouBadge(settings) } : null,
              // No badge while the last status read failed, rather than a stale one.
              [AS_ME_ANCHOR]:
                !status || unknown ? null : status.paused ? { text: "Paused" } : { text: "On", dot: true },
              [APPROVALS_ANCHOR]: level ? { text: approvalsTitle(level) } : null,
            }}
          />

          <div className="@container">
            <Panel
              id={AS_YOU_ANCHOR}
              hidden={tab !== AS_YOU_ANCHOR}
              lede="It writes email in your voice from your own mailbox. How far it goes is your call."
            >
              {delegation.state === "loading" && <PageSpinner />}
              {delegation.state === "error" && (
                <SettingsCard>
                  <p className="text-[15px] text-fg-muted">Couldn&apos;t load this setting.</p>
                </SettingsCard>
              )}
              {delegation.state === "hidden" && (
                <SettingsCard>
                  <p className="text-[15px] text-fg-muted leading-relaxed">
                    As you isn&apos;t available to you here. The owner of this Open Executive can turn it on for team
                    members.
                  </p>
                </SettingsCard>
              )}
              {settings && (
                <ActAsMeCard
                  load={delegation}
                  level={level}
                  side={
                    <>
                      {settings.team && <TeamSection team={settings.team} onSettings={delegation.setSettings} />}
                      {settings.handle_it?.enabled && <SentCard handleIt={settings.handle_it} />}
                    </>
                  }
                />
              )}
            </Panel>

            <Panel
              id={AS_ME_ANCHOR}
              hidden={tab !== AS_ME_ANCHOR}
              lede="In its own name, without being asked. Every day before your brief, and when something comes up, it follows up with people, books meetings, starts workflows, assigns tasks and makes changes in your apps. People can see it's the Executive."
            >
              <div className="grid gap-4 @4xl:grid-cols-2 @4xl:items-start">
                <AsMeCard lead={lead.lead} state={lead.state} setLead={lead.setLead} level={level} />
              </div>
            </Panel>

            <Panel
              id={APPROVALS_ANCHOR}
              hidden={tab !== APPROVALS_ANCHOR}
              lede="One setting for both As you and As me. It decides what goes on its own and what comes to you as an Approve card on Home, the same in the app, email and every chat app."
            >
              <BeforeAnythingCard
                approvals={approvals}
                lead={lead.lead}
                setLead={lead.setLead}
                disclosure={settings?.disclosure ?? null}
                onDelegation={delegation.setSettings}
              />
            </Panel>
          </div>
        </div>
      </main>
      {wide && open && <WhatItDoesAside onClose={() => setOpen(false)} />}
      {!wide && sheet && <WhatItDoesSheet onClose={closeSheet} />}
    </div>
  );
}

type Badge = { text: string; dot?: boolean } | null;

// The tab row: each tab's name and where it stands. Arrow keys, Home and End
// move between tabs.
function DelegateTabs({
  current,
  onPick,
  badges,
}: {
  current: DelegateTab;
  onPick: (tab: DelegateTab) => void;
  badges: Record<DelegateTab, Badge>;
}) {
  const refs = useRef<Partial<Record<DelegateTab, HTMLButtonElement | null>>>({});
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const at = DELEGATE_TABS.indexOf(current);
    const to =
      event.key === "ArrowRight"
        ? (at + 1) % DELEGATE_TABS.length
        : event.key === "ArrowLeft"
          ? (at - 1 + DELEGATE_TABS.length) % DELEGATE_TABS.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? DELEGATE_TABS.length - 1
              : -1;
    if (to < 0) return;
    event.preventDefault();
    const next = DELEGATE_TABS[to];
    onPick(next);
    refs.current[next]?.focus();
  };
  return (
    <div
      role="tablist"
      aria-label="Delegate"
      onKeyDown={move}
      className="mt-6 -mx-4 flex gap-1 overflow-x-auto border-b border-line px-4 sm:mx-0 sm:px-0"
    >
      {DELEGATE_TABS.map((id) => {
        const on = id === current;
        const badge = badges[id];
        return (
          <button
            key={id}
            ref={(el) => {
              refs.current[id] = el;
            }}
            type="button"
            role="tab"
            id={`${id}-tab`}
            aria-selected={on}
            aria-controls={id}
            tabIndex={on ? 0 : -1}
            onClick={() => onPick(id)}
            className={`-mb-px flex flex-none items-center gap-2 border-b-2 px-3 sm:px-4 pb-3 pt-2 text-[15px] ${
              on ? "border-accent font-semibold text-fg" : "border-transparent text-fg-muted hover:text-fg"
            }`}
          >
            {TAB_NAME[id]}
            {badge && (
              <span
                className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
                  on ? "bg-accent/10 text-accent" : "bg-surface-overlay text-fg-subtle"
                }`}
              >
                {badge.dot && <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden="true" />}
                {badge.text}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// All three stay mounted, so a half-written edit on one tab is still there
// when you come back to it.
function Panel({
  id,
  hidden,
  lede,
  children,
}: {
  id: DelegateTab;
  hidden: boolean;
  lede: string;
  children: ReactNode;
}) {
  return (
    <section id={id} role="tabpanel" aria-labelledby={`${id}-tab`} hidden={hidden} className="mt-6">
      <p className="max-w-3xl text-[15px] text-fg-muted leading-relaxed">{lede}</p>
      <div className="mt-5 space-y-4">{children}</div>
    </section>
  );
}
