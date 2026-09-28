"use client";

import type { SettingsSectionDef } from "@/components/shell/navConfig";

// The Settings page's section nav: a sticky list beside the sections on
// `lg` and up, a sticky row of chips under the heading below that. Links
// are real `#id` anchors, so they work without JavaScript; with it, the
// page scrolls smoothly and keeps the hash in the address bar.
export default function SettingsNav({
  sections,
  active,
  onJump,
}: {
  sections: SettingsSectionDef[];
  active: string | null;
  onJump: (id: string) => void;
}) {
  const link = (section: SettingsSectionDef, className: string) => {
    const current = active === section.id;
    return (
      <a
        key={section.id}
        href={`#${section.id}`}
        aria-current={current ? "location" : undefined}
        onClick={(e) => {
          e.preventDefault();
          onJump(section.id);
        }}
        className={`${className} transition-colors ${
          current ? "bg-indigo-500/10 text-indigo-400" : "text-fg-muted hover:text-fg hover:bg-surface-overlay/60"
        }`}
      >
        {section.label}
      </a>
    );
  };

  return (
    <>
      <aside className="hidden lg:block lg:sticky lg:top-6 lg:self-start">
        <nav aria-label="Settings sections" className="space-y-0.5">
          {sections.map((s) => link(s, "block px-2.5 py-1.5 rounded-lg text-xs"))}
        </nav>
      </aside>
      <nav
        aria-label="Settings sections"
        className="lg:hidden sticky top-0 z-10 -mx-4 sm:-mx-6 px-4 sm:px-6 py-2 bg-surface border-b border-line flex gap-1.5 overflow-x-auto"
      >
        {sections.map((s) => link(s, "flex-shrink-0 px-3 py-1.5 rounded-md text-xs whitespace-nowrap"))}
      </nav>
    </>
  );
}
