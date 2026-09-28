"use client";

import { useCallback, useEffect, useState, type RefObject } from "react";

// Which of the sections `ids` is in view inside the scroller `root`, for an
// in-page nav to highlight, plus `jumpTo` for its links. Same observer band
// as the Architecture and User Guide pages (a section is "active" while it
// crosses the 20-30% mark from the top), but with the page's own scroller as
// the root so the band is measured against it, not the whole viewport.
export function useActiveSection(
  ids: readonly string[],
  root: RefObject<HTMLElement | null>,
): { active: string | null; jumpTo: (id: string) => void } {
  const [active, setActive] = useState<string | null>(ids[0] ?? null);
  // A section that mounts late (Act as me, after its fetch) must be
  // observed too, so re-run whenever the list changes.
  const key = ids.join("|");

  useEffect(() => {
    const list = key ? key.split("|") : [];
    const scroller = root.current;
    // Several short sections can sit in the band at once (the top of the
    // page, say): the first in page order is the one in view.
    const inBand = new Set<string>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) inBand.add(e.target.id);
          else inBand.delete(e.target.id);
        }
        const first = list.find((id) => inBand.has(id));
        if (first) setActive(first);
      },
      { root: scroller, rootMargin: "-20% 0px -70% 0px", threshold: 0 },
    );
    for (const id of list) {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    }
    // A short last section never reaches the band, so at the very bottom
    // the last section is the one in view.
    const onScroll = () => {
      if (!scroller) return;
      if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) {
        setActive(list[list.length - 1] ?? null);
      }
    };
    scroller?.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      observer.disconnect();
      scroller?.removeEventListener("scroll", onScroll);
    };
  }, [key, root]);

  const jumpTo = useCallback((id: string) => {
    setActive(id);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    window.history.replaceState(null, "", `#${id}`);
  }, []);

  return { active, jumpTo };
}
