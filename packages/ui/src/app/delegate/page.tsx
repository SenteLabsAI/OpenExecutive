"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { WhatItDoesAside, WhatItDoesSheet, useIsWide, useWhatItDoesOpen } from "@/components/delegate/WhatItDoesPanel";
import ActAsMeCard, { TeamSection, VoiceSection } from "@/components/settings/ActAsMeCard";
import AdvancedFold from "@/components/settings/AdvancedFold";
import AsMeCard, { useTakeTheLead } from "@/components/settings/AsMeCard";
import BeforeAnythingCard from "@/components/settings/BeforeAnythingCard";
import { AsYouTrainingCard, useJumpTo } from "@/components/settings/ReachCard";
import ReplyDisclosureCard from "@/components/settings/ReplyDisclosureCard";
import SentCard from "@/components/settings/SentCard";
import SettingsCard from "@/components/settings/SettingsCard";
import { LearnedCard } from "@/components/settings/TrainingCard";
import { useDelegation } from "@/components/settings/useDelegation";
import { getOutsideContent, type OutsideContentSetting } from "@/lib/api";
import { onApprovalsChanged } from "@/lib/approvalsLevel";
import { APPROVALS_ANCHOR, AS_ME_ANCHOR, AS_YOU_ANCHOR } from "@/lib/reach";
import { PageSpinner } from "@/components/ui/Spinner";

// Delegate: one page, three blocks. As you (Act as me): what it writes and
// sends in your name, from your own mailbox. As me (Take the lead): what it
// does in its own name without being asked. Before anything goes out: the
// one Approvals setting for both, and your rules. In training, on either
// side, holds everything for you first. What it does for you sits beside the
// page (to the right on a wide screen, a sheet on a narrow one).

export default function DelegatePage() {
  const delegation = useDelegation();
  const lead = useTakeTheLead();
  const [approvals, setApprovals] = useState<OutsideContentSetting | null>(null);
  const [open, setOpen] = useWhatItDoesOpen();
  const wide = useIsWide();
  const [sheet, setSheet] = useState(false);
  const closeSheet = useCallback(() => setSheet(false), []);
  useJumpTo(AS_YOU_ANCHOR);
  useJumpTo(AS_ME_ANCHOR);
  useJumpTo(APPROVALS_ANCHOR);

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
        <div className="max-w-3xl mx-auto px-4 sm:px-6 pt-6 sm:pt-8 pb-16">
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

          <div className="mt-6 grid gap-4 sm:grid-cols-2">
            <Example
              tone="you"
              title="As you looks like this"
              from="Olivia Owner"
              address="olivia@yourcompany.example"
              to="to Dana Park"
              body="Dana, Thursday at 2 works. Send the deck ahead and I'll have notes ready. Olivia"
            >
              It wrote this in your voice. <b className="font-semibold text-fg">You pressed Send</b>, or it sent it
              for you if you let it. Dana sees only you.
            </Example>
            <Example
              tone="me"
              title="As me looks like this"
              from="Executive, Your Company"
              address="executive@yourcompany.example"
              to="to Sam Teammate"
              body="Hi Sam, I'm Olivia's Executive. Checking in on the proposal we sent last week. Anything you need from us to decide?"
            >
              It did this itself, in its own name. <b className="font-semibold text-fg">Approvals decided</b> whether
              it asked you first. Sam knows it&apos;s your Executive.
            </Example>
          </div>

          <Block id={AS_YOU_ANCHOR} title="As you" tag="Act as me" lede="It writes email in your voice from your own mailbox. How far it goes is your call.">
            {delegation.state === "loading" && <PageSpinner />}
            {delegation.state === "error" && (
              <SettingsCard>
                <p className="text-[15px] text-fg-muted">Couldn&apos;t load this setting.</p>
              </SettingsCard>
            )}
            {delegation.state === "hidden" && (
              <SettingsCard>
                <p className="text-[15px] text-fg-muted leading-relaxed">
                  Act as me isn&apos;t available to you here. The owner of this Open Executive can turn it on for team
                  members.
                </p>
              </SettingsCard>
            )}
            {settings && (
              <ActAsMeCard load={delegation} level={level}>
                {settings.enabled && (
                  <AsYouTrainingCard settings={settings} onSettings={delegation.setSettings} />
                )}
                {settings.training && <LearnedCard training={settings.training} onSettings={delegation.setSettings} />}
                {settings.handle_it?.enabled && <SentCard handleIt={settings.handle_it} />}
              </ActAsMeCard>
            )}
          </Block>

          <Block
            id={AS_ME_ANCHOR}
            title="As me"
            tag="Take the lead"
            lede="In its own name, without being asked. Every day before your brief it looks over Home, open tasks, alerts and what people said, and acts: it follows up with people, books or moves meetings, starts workflows, assigns tasks and makes changes in your connected apps. New mail or a chat message wakes it for another look. People can see it's the Executive."
          >
            <AsMeCard lead={lead.lead} state={lead.state} setLead={lead.setLead} level={level} />
          </Block>

          <Block
            id={APPROVALS_ANCHOR}
            title="Before anything goes out"
            tag="Approvals"
            lede="One setting for both. It decides what goes on its own and what comes to you as an Approve card on Home, the same in the app, email and every chat app."
          >
            <BeforeAnythingCard approvals={approvals} lead={lead.lead} setLead={lead.setLead} />
          </Block>

          {settings && (
            <div className="mt-8">
              <AdvancedFold
                id="delegate-advanced"
                summary={[
                  "How I write",
                  settings.disclosure?.enabled ? "Say it was written with AI" : null,
                  settings.team ? "Let team members use it" : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              >
                <VoiceSection
                  connected={settings.gmail?.status === "connected"}
                  outlook={settings.gmail?.provider === "microsoft"}
                />
                {settings.disclosure?.enabled && (
                  <ReplyDisclosureCard
                    disclosure={settings.disclosure}
                    onSettings={delegation.setSettings}
                    title="Say it was written with AI"
                  />
                )}
                {settings.team && <TeamSection team={settings.team} onSettings={delegation.setSettings} />}
              </AdvancedFold>
            </div>
          )}
        </div>
      </main>
      {wide && open && <WhatItDoesAside onClose={() => setOpen(false)} />}
      {!wide && sheet && <WhatItDoesSheet onClose={closeSheet} />}
    </div>
  );
}

function Block({
  id,
  title,
  tag,
  lede,
  children,
}: {
  id: string;
  title: string;
  tag: string;
  lede: string;
  children: ReactNode;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="mt-10 scroll-mt-24 border-t border-line pt-8">
      <div className="flex items-baseline gap-3">
        <h2 id={`${id}-title`} className="text-xl sm:text-2xl font-semibold tracking-tight text-fg">
          {title}
        </h2>
        <span className="text-xs text-fg-subtle">{tag}</span>
      </div>
      <p className="mt-2 text-[15px] text-fg-muted leading-relaxed">{lede}</p>
      <div className="mt-5 space-y-5">{children}</div>
    </section>
  );
}

// One made-up email, as the person on the other end sees it.
function Example({
  tone,
  title,
  from,
  address,
  to,
  body,
  children,
}: {
  tone: "you" | "me";
  title: string;
  from: string;
  address: string;
  to: string;
  body: string;
  children: ReactNode;
}) {
  const head = tone === "you" ? "text-indigo-700 dark:text-indigo-300" : "text-orange-700 dark:text-orange-300";
  const avatar =
    tone === "you"
      ? "bg-indigo-100 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300"
      : "bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300";
  const initials = from
    .split(/[\s,]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");
  return (
    <div className="rounded-2xl border border-line bg-surface-elevated p-5">
      <h3 className={`text-xs font-semibold uppercase tracking-wider ${head}`}>{title}</h3>
      <div className="mt-3 rounded-xl border border-line bg-surface px-3.5 py-3 text-sm leading-relaxed">
        <div className="flex items-center gap-2.5">
          <span aria-hidden className={`flex h-7 w-7 flex-none items-center justify-center rounded-full text-xs font-bold ${avatar}`}>
            {initials}
          </span>
          <div className="min-w-0">
            <span className="block truncate font-semibold text-fg">
              {from} <span className="font-normal text-fg-subtle">&lt;{address}&gt;</span>
            </span>
            <span className="block text-xs text-fg-subtle">{to}</span>
          </div>
        </div>
        <p className="mt-2 text-fg-muted">{body}</p>
      </div>
      <p className="mt-3 text-sm text-fg-muted leading-relaxed">{children}</p>
    </div>
  );
}
