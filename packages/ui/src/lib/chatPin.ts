// Scrolling for the chat: each message you send moves to the top of the
// view, with empty room under it for the reply to fill (as ChatGPT and
// Claude do). Pure measurements here, so they can be tested without a DOM.

/** Space kept above the pinned message, in px. */
export const PIN_TOP_GAP = 16;

/** How close to the bottom (px) still counts as "at the bottom". */
export const NEAR_BOTTOM_PX = 80;

/**
 * Empty space to add under the chat so the pinned message can sit at the
 * top of the view. `pinnedTop` and `contentBottom` are measured in the same
 * frame (e.g. both from getBoundingClientRect). Shrinks to 0 once the reply
 * below the message is taller than the view.
 */
export function pinSpacerHeight(
  viewportHeight: number,
  pinnedTop: number,
  contentBottom: number,
  gap: number = PIN_TOP_GAP,
): number {
  const below = contentBottom - pinnedTop;
  return Math.max(0, Math.ceil(viewportHeight - gap - below));
}

/**
 * The scrollTop that brings the pinned message to `gap` px under the top of
 * the view. `pinnedTop` and `viewportTop` are viewport coordinates.
 */
export function pinScrollTop(
  currentScrollTop: number,
  pinnedTop: number,
  viewportTop: number,
  gap: number = PIN_TOP_GAP,
): number {
  return Math.max(0, Math.round(currentScrollTop + (pinnedTop - viewportTop) - gap));
}

/** Whether the view sits at (or near) the bottom, so new content should follow. */
export function isNearBottom(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
  slack: number = NEAR_BOTTOM_PX,
): boolean {
  return scrollHeight - (scrollTop + clientHeight) <= slack;
}

/** Index of the newest message you sent, or null when there is none. */
export function lastUserIndex(messages: ReadonlyArray<{ role: string }>): number | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "user") return i;
  }
  return null;
}
