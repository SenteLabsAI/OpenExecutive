"use client";

import { useState } from "react";

import Icon from "@/components/Icon";

// A page's own side menu (the section list on the Guide, the agent list on
// the Agent Council, the source tree in the Knowledge base). From `md` up it
// is a fixed-width column beside the page. Below `md` a full column would
// squeeze the page into a strip a few words wide, so it folds into a bar at
// the top that opens the same menu over the page and closes again once the
// selection changes, or when an item marked `data-closes-nav` is tapped
// (re-tapping the current item changes nothing, so the key alone misses it).
//
// The children render once, in one place, at every width, so element ids and
// observers inside them behave the same on phones and desktops. The parent
// must stack on phones and sit side by side from `md`
// (`flex flex-col md:flex-row`).
export default function PageSideNav({
  label,
  current,
  closeKey,
  className = "",
  children,
}: {
  /** What the menu is, e.g. "Sections". Shown on the phone bar. */
  label: string;
  /** The selected item's name, shown on the phone bar next to the label. */
  current?: string;
  /** Changes whenever the selection does; the phone menu closes on change. */
  closeKey?: string;
  /** Width, background and padding for the desktop column, e.g. "md:w-52". */
  className?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  // Close when the selection changes, adjusted during render rather than in
  // an effect so the menu never paints open over the new selection.
  const [seenKey, setSeenKey] = useState(closeKey);
  if (closeKey !== seenKey) {
    setSeenKey(closeKey);
    setOpen(false);
  }

  return (
    <div className="relative flex-shrink-0 md:contents">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="md:hidden w-full min-h-touch flex items-center gap-2 px-4 py-2 border-b border-line bg-surface-elevated text-left text-sm cursor-pointer"
      >
        <span className="text-fg-subtle flex-shrink-0">{label}</span>
        {current && <span className="font-medium text-fg truncate">{current}</span>}
        <Icon
          name="chevron-right"
          size="w-4 h-4"
          className={`ml-auto flex-shrink-0 text-fg-muted transition-transform ${open ? "rotate-90" : ""}`}
        />
      </button>
      <aside
        onClick={(e) => {
          if ((e.target as Element).closest("[data-closes-nav]")) setOpen(false);
        }}
        className={`${
          open ? "flex" : "hidden"
        } md:flex flex-col flex-shrink-0 overflow-y-auto border-line absolute md:static inset-x-0 top-full z-20 max-h-[70vh] md:max-h-none border-b md:border-b-0 md:border-r shadow-lg md:shadow-none ${className}`}
      >
        {children}
      </aside>
    </div>
  );
}
