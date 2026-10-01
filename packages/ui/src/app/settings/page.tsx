"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import VoicePicker from "@/components/executive/VoicePicker";
import Icon from "@/components/Icon";
import ActAsMeCard from "@/components/settings/ActAsMeCard";
import SettingsNav from "@/components/settings/SettingsNav";
import SettingsSection from "@/components/settings/SettingsSection";
import WorkspaceCard from "@/components/settings/WorkspaceCard";
import { advancedItemsByGroup, SETTINGS_SECTIONS } from "@/components/shell/navConfig";
import { useActiveSection } from "@/lib/useActiveSection";

// Settings — the configuration that lives outside the day-to-day nav, as
// one page of sections (Executive, Workspace, Act as me, Tools) with an
// in-page nav to jump between them. Each section id is a hash a link can
// land on; the Tools section points at the admin / power-user pages
// (ADVANCED_ITEMS), grouped by what you'd use them for.
export default function SettingsPage() {
  const mainRef = useRef<HTMLElement>(null);
  // Act as me decides for itself whether it is on the page (owner only).
  const [actAsMe, setActAsMe] = useState(false);
  const sections = useMemo(
    () => SETTINGS_SECTIONS.filter((s) => s.id !== "act-as-me" || actAsMe),
    [actAsMe],
  );
  const ids = useMemo(() => sections.map((s) => s.id), [sections]);
  const { active, jumpTo } = useActiveSection(ids, mainRef);

  // A link with a hash lands on its section. The browser does this itself
  // for a section present at first paint; Act as me mounts after its fetch,
  // so scroll to the hash once its section exists.
  const scrolledToHash = useRef(false);
  useEffect(() => {
    if (scrolledToHash.current) return;
    const id = window.location.hash.slice(1);
    if (!id || !ids.includes(id as (typeof ids)[number])) return;
    const el = document.getElementById(id);
    if (!el) return;
    scrolledToHash.current = true;
    el.scrollIntoView({ block: "start" });
  }, [ids]);

  return (
    <main ref={mainRef} className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-3xl lg:max-w-4xl mx-auto px-4 sm:px-6 pt-8 pb-16">
        <h1 className="text-xl font-semibold text-fg">Settings &amp; advanced</h1>
        <p className="mt-1 text-sm text-fg-muted">
          How the Executive runs, who it runs for, and the tools that sit outside the
          day-to-day workspace nav.
        </p>

        <div className="mt-6 lg:grid lg:grid-cols-[176px_minmax(0,1fr)] lg:gap-10">
          <SettingsNav sections={sections} active={active} onJump={jumpTo} />

          <div className="mt-4 lg:mt-0 space-y-8 min-w-0">
            <SettingsSection
              id="executive"
              title="Executive"
              description="Whether the Executive is doing its own work — briefs, nudges, monitoring, inbox, workflow timers — or holding it, and the voice it answers in."
            >
              <ExecutiveRunSwitch variant="card" />
              <div className="mt-6">
                <h3 className="text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
                  Voice
                </h3>
                <VoicePicker variant="card" />
              </div>
            </SettingsSection>

            <SettingsSection
              id="workspace"
              title="Workspace"
              description="Who Open Executive is for, and the time zone your briefs run in."
            >
              <WorkspaceCard />
            </SettingsSection>

            {/* Renders nothing for anyone who can't have Act as me (only the owner can). */}
            <ActAsMeCard onVisible={setActAsMe} />

            <SettingsSection
              id="tools"
              title="Tools"
              description="Diagnostics, configuration and reference pages. Each opens its own screen."
            >
              <div className="space-y-5">
                {advancedItemsByGroup().map((group) => (
                  <div key={group.key}>
                    <h3
                      id={`tools-${group.key}`}
                      className="text-[10px] font-semibold uppercase tracking-widest text-fg-subtle"
                    >
                      {group.label}
                    </h3>
                    <ul className="mt-1 divide-y divide-line">
                      {group.items.map((item) => (
                        <li key={item.href}>
                          <Link
                            href={item.href}
                            className="group flex items-center gap-3 py-2.5 -mx-2 px-2 rounded-lg hover:bg-surface-overlay transition-colors"
                          >
                            <span className="text-fg-muted group-hover:text-fg transition-colors">
                              <Icon name={item.icon} size="w-4 h-4" />
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="block text-sm text-fg">{item.label}</span>
                              <span className="block text-xs text-fg-muted">
                                {item.description}
                              </span>
                            </span>
                            <Icon
                              name="chevron-right"
                              size="w-3.5 h-3.5"
                              className="text-fg-subtle group-hover:text-fg transition-colors"
                            />
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </SettingsSection>
          </div>
        </div>
      </div>
    </main>
  );
}
