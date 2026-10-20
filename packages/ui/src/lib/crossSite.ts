// Refuses state-changing requests to the backend proxy that another site made
// the browser send.
//
// The session cookie is SameSite=Lax, which stops other websites but not
// other pages on the same *site* — and to a browser every localhost port is
// one site. So any other local dev server (or a page a local tool serves)
// could otherwise post a form to /api/backend/* with the owner's cookie
// attached; the multipart routes (chat uploads, documents) would accept it.
// `Sec-Fetch-Site` is set by the browser itself and a page cannot forge it.
// A request without it (a non-browser client, or a browser from before 2023)
// is left to SameSite alone, as before.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/crossSite.test.mjs).

const READ_ONLY_METHODS = new Set(["GET", "HEAD"]);

/**
 * Sec-Fetch-Site values for a request the page's own origin (or a typed URL)
 * made. The API applies the same rule under local login (api/main.py
 * _OWN_PAGE_FETCH_SITES); scripts/crossSite.test.mjs fails if they drift.
 */
export const OWN_PAGE_FETCH_SITES: ReadonlySet<string> = new Set(["same-origin", "none"]);

export function isCrossSiteWrite(method: string, secFetchSite: string | null): boolean {
  if (READ_ONLY_METHODS.has(method.toUpperCase()) || secFetchSite === null) return false;
  return !OWN_PAGE_FETCH_SITES.has(secFetchSite.trim().toLowerCase());
}
