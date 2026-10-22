// Host origins (docs/auth.md, "Host origins"): other apps that may show Open
// Executive's pages in a frame and call a short list of its routes from their
// own pages, with the signed-in person's cookie. OE_HOST_ORIGINS lists them,
// exactly (scheme, host and port). They must be on the same site as this app
// (sibling hosts under one registrable domain), or the browser won't send
// the session cookie with their calls.
//
// A host's page runs code Open Executive doesn't control, so what it may
// call is the host list (host-list.json): reading, chatting, alerts and the
// watch list, never approving a card, sending, delegation, settings or
// People. Everything here is a pure rule; the middleware and the backend
// proxy apply it.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/hostOrigins.test.mjs).

export type HostCall = readonly [method: string, pattern: string];

export type HostOrigins = {
  origins: readonly string[];
  // Why OE_HOST_ORIGINS couldn't be used; with an error, no origin is a host.
  error: string | null;
};

const ORIGIN_RE = /^https?:\/\/[a-z0-9.-]+(?::\d{1,5})?$/;

/**
 * OE_HOST_ORIGINS (comma- or space-separated origins) → the origins. Each is
 * `https://host[:port]` (or `http://` for a name under `localhost`), with no
 * path, query or wildcard. Anything malformed refuses the whole setting:
 * a typo must not quietly widen or narrow what is trusted.
 */
export function parseHostOrigins(raw: string | undefined): HostOrigins {
  const entries = (raw ?? "")
    .split(/[\s,]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
  const origins: string[] = [];
  for (const entry of entries) {
    const origin = entry.toLowerCase().replace(/\/$/, "");
    if (!ORIGIN_RE.test(origin)) {
      return { origins: [], error: `OE_HOST_ORIGINS: ${JSON.stringify(entry)} is not an origin like https://app.example.com` };
    }
    const host = origin.replace(/^https?:\/\//, "").replace(/:\d+$/, "");
    if (origin.startsWith("http://") && host !== "localhost" && !host.endsWith(".localhost")) {
      return { origins: [], error: `OE_HOST_ORIGINS: ${entry} must be https (plain http only for a name under localhost)` };
    }
    if (!origins.includes(origin)) origins.push(origin);
  }
  return { origins, error: null };
}

/** The framing rule: who may show these pages in a frame. */
export function frameAncestors(origins: readonly string[]): string {
  return ["frame-ancestors 'self'", ...origins].join(" ");
}

/** The request's path under /api/backend as segments, or null for any other path. */
export function backendSegments(pathname: string): string[] | null {
  const prefix = "/api/backend/";
  if (!pathname.startsWith(prefix)) return null;
  const segments: string[] = [];
  for (const raw of pathname.slice(prefix.length).split("/")) {
    try {
      segments.push(decodeURIComponent(raw));
    } catch {
      return [];
    }
  }
  return segments;
}

/**
 * Whether `method` on the API path `segments` (decoded, as the proxy gets
 * them) is on the host list. `{name}` stands for one segment, never empty,
 * `.` or `..`, and never holding a slash, backslash, `?`, `#`, `%` or space,
 * so a call can't step out of the route it names.
 */
export function hostCallAllowed(calls: readonly HostCall[], method: string, segments: readonly string[]): boolean {
  const verb = method.toUpperCase();
  return calls.some(([callMethod, pattern]) => {
    if (callMethod !== verb) return false;
    const parts = pattern.split("/").slice(1);
    if (parts.length !== segments.length) return false;
    return parts.every((part, i) => {
      const segment = segments[i];
      if (/^\{[a-z_]+\}$/.test(part)) {
        return segment !== "." && segment !== ".." && /^[^/\\?#%\s]+$/.test(segment);
      }
      return part === segment;
    });
  });
}

export type HostVerdict =
  // Not from a host origin: the app's own pages (and everything else) are
  // judged as before (lib/crossSite.ts).
  | { kind: "own" }
  // A call from a host origin that is on the host list.
  | { kind: "host"; origin: string }
  // From a host origin but not allowed. `origin` gets CORS on the refusal,
  // so the host's page can read why.
  | { kind: "refused"; origin: string; reason: "not_same_site" | "not_on_host_list" };

/**
 * How to treat a request to the backend proxy. Only a request whose Origin
 * is exactly a listed host origin is a host call, and only with
 * `Sec-Fetch-Site: same-site` (set by the browser, which a page can't forge):
 * anything else naming a host origin (no Sec-Fetch-Site, a client that isn't
 * a browser, a host on another site) is refused.
 */
export function judgeHostRequest(
  request: { origin: string | null; secFetchSite: string | null; method: string; segments: readonly string[] },
  hosts: HostOrigins,
  calls: readonly HostCall[],
): HostVerdict {
  const origin = request.origin?.trim().toLowerCase() ?? "";
  if (!origin || !hosts.origins.includes(origin)) return { kind: "own" };
  if ((request.secFetchSite ?? "").trim().toLowerCase() !== "same-site") {
    return { kind: "refused", origin, reason: "not_same_site" };
  }
  if (!hostCallAllowed(calls, request.method, request.segments)) {
    return { kind: "refused", origin, reason: "not_on_host_list" };
  }
  return { kind: "host", origin };
}

/** CORS for one exact host origin, with credentials (the session cookie). */
export function corsHeaders(origin: string): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Credentials": "true",
    Vary: "Origin",
  };
}

// The request headers a host's page may send: what its fetch and form
// uploads set. Anything else asked for is left out of the answer, so the
// browser doesn't send the call.
const ALLOWED_REQUEST_HEADERS = new Set(["accept", "content-type", "last-event-id"]);

/**
 * The answer to a preflight (OPTIONS, which carries no cookie) from a host
 * origin, or null when the request is no preflight from one. It names only
 * the method asked for, and only when that call is on the host list.
 */
export function hostPreflight(
  request: {
    origin: string | null;
    secFetchSite: string | null;
    method: string;
    segments: readonly string[];
    requestMethod: string | null;
    requestHeaders: string | null;
  },
  hosts: HostOrigins,
  calls: readonly HostCall[],
): { status: number; headers: Record<string, string> } | null {
  if (request.method.toUpperCase() !== "OPTIONS") return null;
  const asked = (request.requestMethod ?? "").trim().toUpperCase();
  const verdict = judgeHostRequest({ ...request, method: asked || "OPTIONS" }, hosts, calls);
  if (verdict.kind === "own") return null;
  if (verdict.kind === "refused" || !asked) return { status: 403, headers: { Vary: "Origin" } };
  const headers = (request.requestHeaders ?? "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter((h) => ALLOWED_REQUEST_HEADERS.has(h));
  return {
    status: 204,
    headers: {
      ...corsHeaders(verdict.origin),
      "Access-Control-Allow-Methods": asked,
      ...(headers.length ? { "Access-Control-Allow-Headers": headers.join(", ") } : {}),
      "Access-Control-Max-Age": "600",
    },
  };
}

// The browser's own headers about where a request came from. Once the proxy
// has judged a host call it drops them: the API's rule for local login
// (api/main.py _OWN_PAGE_FETCH_SITES) would refuse a same-site write, and
// the call's `host` surface tells the API what it needs to know.
export function isBrowserOriginHeader(name: string): boolean {
  const lower = name.toLowerCase();
  return lower === "origin" || lower === "referer" || lower.startsWith("sec-fetch-");
}
