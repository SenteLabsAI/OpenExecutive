// Keeping the Briefing's "What's going on" header current.
//
// /today serves the cached header and, when the picture has moved, rewrites
// it in the background (`narrative_stale: true` on the response). The page
// used to fetch once on mount, so it always showed the header from the
// previous visit. These helpers drive a short re-poll while a rewrite is in
// flight, a refresh when the tab comes back, and the "Updated …" stamp.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/narrativeFreshness.test.mjs).

/** Delays before each re-poll while the header is being rewritten. The model
 * call takes a few seconds; after the last one the page waits for the next
 * focus or periodic refresh rather than polling forever. */
export const NARRATIVE_REPOLL_DELAYS_MS = [4_000, 10_000, 20_000] as const;

/** How often an open, visible Briefing re-pulls /today on its own. */
export const BRIEFING_REFRESH_INTERVAL_MS = 5 * 60_000;

/** A tab coming back to the foreground refreshes at most this often. */
export const FOCUS_REFRESH_MIN_GAP_MS = 60_000;

/** The delay before re-poll number `attempt` (0-based), or null when the
 * page should stop polling. */
export function narrativeRepollDelay(attempt: number): number | null {
  if (!Number.isInteger(attempt) || attempt < 0) return null;
  return NARRATIVE_REPOLL_DELAYS_MS[attempt] ?? null;
}

/** Whether a tab that just became visible should re-pull /today. */
export function shouldRefreshOnFocus(lastFetchMs: number, nowMs: number): boolean {
  return nowMs - lastFetchMs >= FOCUS_REFRESH_MIN_GAP_MS;
}

/** "Updated just now" / "Updated 12 min ago" / "Updated 9:42 AM" — or null
 * when there is no (readable) timestamp. */
export function narrativeUpdatedLabel(generatedAt: string | null | undefined, now: Date): string | null {
  if (!generatedAt) return null;
  const written = new Date(generatedAt);
  const ms = now.getTime() - written.getTime();
  if (Number.isNaN(ms)) return null;
  if (ms < 60_000) return "Updated just now";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `Updated ${minutes} min ago`;
  const hours = written.getHours();
  const h12 = hours % 12 === 0 ? 12 : hours % 12;
  const mm = String(written.getMinutes()).padStart(2, "0");
  return `Updated ${h12}:${mm} ${hours < 12 ? "AM" : "PM"}`;
}
