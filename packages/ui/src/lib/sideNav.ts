// Whether a page's phone side menu (components/shell/PageSideNav.tsx) is open
// after something happens. Kept free of React and the DOM so `npm test` can
// run it under `node --experimental-strip-types` (scripts/sideNav.test.mjs).

export type SideNavEvent =
  /** The bar button above the menu was tapped. */
  | { type: "toggle" }
  /** A tap landed inside the menu; `closesNav` when it hit an item marked `data-closes-nav`. */
  | { type: "menu-click"; closesNav: boolean }
  /** A tap landed on the page behind the open menu. */
  | { type: "outside" }
  /** A key was pressed while the menu was open. */
  | { type: "key"; key: string }
  /** The page's selection changed (its `closeKey`). */
  | { type: "selection-changed" };

export function nextSideNavOpen(open: boolean, event: SideNavEvent): boolean {
  switch (event.type) {
    case "toggle":
      return !open;
    case "menu-click":
      // Re-tapping the current item changes no selection, so the item itself
      // closes the menu. Other taps (a "Show all" toggle, a disclosure) keep it open.
      return event.closesNav ? false : open;
    case "outside":
    case "selection-changed":
      return false;
    case "key":
      return event.key === "Escape" ? false : open;
  }
}
