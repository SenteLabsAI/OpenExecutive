"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";

import { nextMenuIndex } from "@/lib/menuNav";

// The ⋯ menu: where a card's rarer actions live (edit, dismiss, delete...)
// so the card shows one primary button. Items are buttons or links.
// Keyboard: Enter/Space or ArrowDown opens it, arrows move, Escape closes
// and returns focus to the ⋯ button. A click outside closes it.

export interface OverflowItem {
  label: string;
  onSelect?: () => void;
  href?: string;
  /** Opens in a new tab (links only). */
  external?: boolean;
  danger?: boolean;
  disabled?: boolean;
}

export default function OverflowMenu({
  items,
  label = "More actions",
  size = "md",
  align = "right",
  placement = "down",
}: {
  items: OverflowItem[];
  /** Accessible name of the ⋯ button. */
  label?: string;
  size?: "md" | "sm";
  align?: "left" | "right";
  /** "up" for a menu at the bottom of the screen (the account menu). */
  placement?: "down" | "up";
}) {
  const [open, setOpen] = useState(false);
  const [focus, setFocus] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLElement | null)[]>([]);
  const menuId = useId();

  const close = (returnFocus: boolean) => {
    setOpen(false);
    setFocus(-1);
    if (returnFocus) buttonRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) close(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  useEffect(() => {
    if (open && focus >= 0) itemRefs.current[focus]?.focus();
  }, [open, focus]);

  if (items.length === 0) return null;
  const disabled = items.map((i) => !!i.disabled);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      if (open) {
        e.stopPropagation();
        close(true);
      }
      return;
    }
    if (e.key === "Tab") {
      close(false);
      return;
    }
    if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setOpen(true);
      setFocus(nextMenuIndex(-1, e.key, disabled));
      return;
    }
    if (open && ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      setFocus((f) => nextMenuIndex(f, e.key, disabled));
    }
  };

  const dim = size === "sm" ? "h-9 w-9" : "h-11 w-11";
  const itemClass = (item: OverflowItem) =>
    `flex w-full items-center px-3.5 py-2.5 text-left text-[15px] rounded-lg transition-colors focus:outline-none focus-visible:bg-surface-hover disabled:opacity-40 disabled:cursor-not-allowed ${
      item.danger ? "text-rose-500 hover:bg-rose-500/10" : "text-fg hover:bg-surface-hover"
    }`;

  return (
    <div ref={rootRef} className="relative inline-flex" onKeyDown={onKeyDown}>
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? close(false) : setOpen(true))}
        className={`${dim} inline-flex items-center justify-center rounded-xl text-fg-muted hover:text-fg hover:bg-surface-overlay border border-transparent hover:border-line transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60`}
      >
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
          <circle cx="5" cy="12" r="1.8" />
          <circle cx="12" cy="12" r="1.8" />
          <circle cx="19" cy="12" r="1.8" />
        </svg>
      </button>
      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label={label}
          className={`absolute z-40 min-w-[13rem] max-w-[calc(100vw-2rem)] rounded-xl border border-line bg-surface-elevated p-1.5 shadow-xl ${
            align === "right" ? "right-0" : "left-0"
          } ${placement === "up" ? "bottom-full mb-1.5" : "top-full mt-1.5"}`}
        >
          {items.map((item, i) => {
            const setRef = (el: HTMLElement | null) => {
              itemRefs.current[i] = el;
            };
            if (item.href && !item.disabled) {
              return (
                <Link
                  key={item.label}
                  ref={setRef}
                  role="menuitem"
                  tabIndex={-1}
                  href={item.href}
                  target={item.external ? "_blank" : undefined}
                  rel={item.external ? "noopener noreferrer" : undefined}
                  onClick={() => close(false)}
                  className={itemClass(item)}
                >
                  {item.label}
                </Link>
              );
            }
            return (
              <button
                key={item.label}
                ref={setRef}
                type="button"
                role="menuitem"
                tabIndex={-1}
                disabled={item.disabled}
                onClick={() => {
                  close(true);
                  item.onSelect?.();
                }}
                className={itemClass(item)}
              >
                {item.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
