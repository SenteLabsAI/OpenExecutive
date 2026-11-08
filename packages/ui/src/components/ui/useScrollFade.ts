"use client";

import { useCallback, useEffect, useState, type CSSProperties, type RefObject } from "react";

// A row that scrolls sideways (tabs on a phone) fades out at an edge that has
// more past it, so a cut-off last tab reads as "there's more" rather than
// as a clipped label. No fade when everything fits.

const FADE = "2.5rem";

export function fadeMask(start: boolean, end: boolean): string | undefined {
  if (!start && !end) return undefined;
  const from = start ? `transparent, black ${FADE}` : "black";
  const to = end ? `black calc(100% - ${FADE}), transparent` : "black";
  return `linear-gradient(to right, ${from}, ${to})`;
}

export function useScrollFade(ref: RefObject<HTMLElement | null>): {
  style: CSSProperties | undefined;
  onScroll: () => void;
} {
  const [edges, setEdges] = useState({ start: false, end: false });
  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const start = el.scrollLeft > 1;
    const end = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((prev) => (prev.start === start && prev.end === end ? prev : { start, end }));
  }, [ref]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    for (const child of Array.from(el.children)) observer.observe(child);
    return () => observer.disconnect();
  }, [ref, measure]);
  // Tabs added, removed or relabelled (another hub, a count that grew)
  // change what overflows without resizing the row: measure after each
  // render too. It only sets state when an edge flips.
  useEffect(measure);
  const mask = fadeMask(edges.start, edges.end);
  return { style: mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined, onScroll: measure };
}
