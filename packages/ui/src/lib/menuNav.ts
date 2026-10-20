// Keyboard movement inside a menu (components/ui/OverflowMenu.tsx), kept
// pure so `npm test` can check it (scripts/menuNav.test.mjs).
//
// `current` is the focused item's index, or -1 when focus is still on the
// button that opened the menu. Disabled items are skipped. Returns the index
// to focus next, or `current` when the key doesn't move focus or nothing can
// take it.
export function nextMenuIndex(
  current: number,
  key: string,
  disabled: readonly boolean[],
): number {
  const n = disabled.length;
  if (n === 0 || disabled.every(Boolean)) return current;
  const step = (from: number, dir: 1 | -1): number => {
    let i = from;
    for (let tries = 0; tries < n; tries++) {
      i = (i + dir + n) % n;
      if (!disabled[i]) return i;
    }
    return current;
  };
  switch (key) {
    case "ArrowDown":
      return step(current < 0 ? -1 : current, 1);
    case "ArrowUp":
      return step(current < 0 ? 0 : current, -1);
    case "Home":
      return step(-1, 1);
    case "End":
      return step(0, -1);
    default:
      return current;
  }
}
