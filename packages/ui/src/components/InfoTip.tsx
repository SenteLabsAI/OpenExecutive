"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import Icon from "./Icon";

interface InfoTipProps {
  /** Tip content — text or rich JSX. */
  children: React.ReactNode;
  /** Optional aria-label for the trigger. Default: "More info". */
  label?: string;
  /** Horizontal alignment of the popover relative to the trigger. */
  align?: "left" | "center" | "right";
}

// Lightweight hover/focus/tap tooltip primitive — used to attach contextual
// explanations to UI labels (e.g. "what is a Department?"). Hand-rolled
// because the codebase has no popover library; follows the same state-driven
// overlay pattern as DebugPanel.
//
// Behaviour:
//  - Opens on hover, focus, OR tap (touch devices have no hover state, so
//    we cannot rely on it alone).
//  - Closes on blur, mouse-leave (with a 120ms grace period so the user
//    can mouse INTO the tip if it has links / interactive content),
//    Escape, and outside-click.
//  - `aria-describedby` links the trigger to the tooltip while open.
export default function InfoTip({
  children,
  label = "More info",
  align = "center",
}: InfoTipProps) {
  const [open, setOpen] = useState(false);
  const tipId = useId();
  const wrapRef = useRef<HTMLSpanElement>(null);
  const closeTimer = useRef<number | null>(null);

  const cancelClose = useCallback(() => {
    if (closeTimer.current != null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);

  // 200ms grace gives the cursor plenty of time to cross the visual gap
  // between trigger and popover before the close fires. Industry standard
  // (Radix, Reach) uses similar deferred-close timing.
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 200);
  }, [cancelClose]);

  // Outside click + Escape — only attach listeners while open so we're not
  // firing on every page interaction in the steady state.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        // Mark it consumed so a page-level Escape handler (e.g. Chat's
        // stop-the-turn shortcut) doesn't also fire on the same keypress.
        e.preventDefault();
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Clear any pending close timer on unmount.
  useEffect(() => () => cancelClose(), [cancelClose]);

  const alignClass =
    align === "left"
      ? "left-0"
      : align === "right"
      ? "right-0"
      : "left-1/2 -translate-x-1/2";

  return (
    <span
      ref={wrapRef}
      className="relative inline-flex items-center"
      onMouseEnter={() => {
        cancelClose();
        setOpen(true);
      }}
      onMouseLeave={scheduleClose}
    >
      <button
        type="button"
        aria-label={label}
        aria-describedby={open ? tipId : undefined}
        aria-expanded={open}
        aria-controls={open ? tipId : undefined}
        onFocus={() => {
          cancelClose();
          setOpen(true);
        }}
        onBlur={scheduleClose}
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
        className="inline-flex items-center justify-center text-fg-subtle hover:text-fg-muted focus:text-fg-muted focus:outline-none focus-visible:ring-1 focus-visible:ring-indigo-400 rounded-full cursor-help"
      >
        <Icon name="info" size="w-3.5 h-3.5" />
      </button>
      {open && (
        <span
          role="tooltip"
          id={tipId}
          onMouseEnter={cancelClose}
          onMouseLeave={scheduleClose}
          className={`absolute top-full mt-1.5 ${alignClass} z-40 w-72 max-w-[80vw] rounded-md border border-line bg-surface-overlay px-3 py-2 text-xs leading-snug text-fg-muted shadow-lg`}
        >
          {children}
        </span>
      )}
    </span>
  );
}
