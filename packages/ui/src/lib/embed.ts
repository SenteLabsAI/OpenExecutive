// Embedded mode: Open Executive's pages inside a host app's frame (a host
// origin, lib/hostOrigins.ts), which owns the navigation, the header and the
// theme around them. docs/auth.md, "Host origins", has the whole picture.
//
// The frame and the host talk in `oe-embed/v1` postMessage messages:
//
//   frame → host  ready      { contract: 1, version }
//   host → frame  init       { contract: 1, theme, font, productName, userHint, nativePaths }
//   host → frame  theme      { theme }                  when the person switches theme
//   frame → host  route      { path, query, title }     on every navigation
//   frame → host  open       { kind: "host", route } | { kind: "native", path }
//   frame → host  badge      { counts }
//   popup → opener signed-in {}                         the popup sign-in is done
//
// Each carries `protocol: "oe-embed/v1"` and `type`. None carries a secret, a
// token or an identity anyone acts on: `userHint` is shown, never trusted.
// The frame takes a message only from its parent window and only from a
// listed host origin, exactly, and validates every field: colours as
// colours, the font stack against a short pattern, the product name as plain
// text of bounded length, native paths against this app's own routes. A
// message that fails any of it is dropped whole. Until a valid `init`
// arrives, the page renders as it would outside a frame.
//
// src/lib/embed-examples.json is a fixed set of example messages with what
// each must parse to; scripts/embed.test.mjs and a host's own tests both
// check it, so the two sides can't drift.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types`.

export const EMBED_PROTOCOL = "oe-embed/v1";
export const EMBED_CONTRACT = 1;

// The colour tokens a host may set (globals.css): each "R G B".
export const THEME_TOKENS = [
  "surface-base",
  "surface-elevated",
  "surface-overlay",
  "surface-input",
  "surface-hover",
  "border-default",
  "border-strong",
  "border-subtle",
  "fg",
  "fg-muted",
  "fg-subtle",
  "fg-inverse",
  "accent",
  "accent-strong",
] as const;
export type ThemeToken = (typeof THEME_TOKENS)[number];

export type EmbedTheme = { mode: "light" | "dark"; tokens: Partial<Record<ThemeToken, string>> };

export type EmbedInit = {
  theme: EmbedTheme;
  // A CSS font-family list, or null to keep this app's.
  font: string | null;
  productName: string;
  // The email of the person the host says is using it: display only.
  userHint: string | null;
  // Paths the host shows natively; the frame asks the host to open those
  // instead of showing them itself.
  nativePaths: string[];
};

export type HostMessage = { type: "init"; init: EmbedInit } | { type: "theme"; theme: EmbedTheme };

// This app's top-level routes (src/app/*); scripts/embed.test.mjs keeps it in
// step. A native path must start with one of them, or be "/".
export const APP_ROUTES = [
  "architecture",
  "artifacts",
  "audit",
  "cards",
  "chats",
  "clients",
  "company-profile",
  "confirm",
  "council",
  "delegate",
  "demo",
  "departments",
  "goals",
  "guide",
  "jobs",
  "knowledge",
  "memories",
  "onboard",
  "people",
  "review",
  "settings",
  "signin",
  "skills",
  "today",
  "watchlist",
] as const;

// Pages that stay in the frame even if the host shows the same place
// natively: a card's own page is where it is approved, in this app's origin.
const NEVER_NATIVE = ["/cards", "/confirm", "/signin"];

const MAX_PRODUCT_NAME = 40;
const MAX_NATIVE_PATHS = 20;
const MAX_EMAIL = 320;
const FONT_RE = /^[A-Za-z0-9 ,'"._-]{1,200}$/;
const NATIVE_PATH_RE = /^\/(?:[a-z0-9-]+(?:\/[a-z0-9-]+)*)?$/;
const EMAIL_RE = /^[^\s@<>"']+@[^\s@<>"']+$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A colour as `#rgb`, `#rrggbb` or `rgb(r, g, b)` → "R G B", else null. */
export function parseColor(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim().toLowerCase();
  let hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(text)?.[1];
  if (hex) {
    if (hex.length === 3) hex = [...hex].map((c) => c + c).join("");
    return [0, 2, 4].map((i) => parseInt(hex!.slice(i, i + 2), 16)).join(" ");
  }
  const rgb = /^rgb\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)$/.exec(text);
  if (rgb) {
    const parts = rgb.slice(1).map(Number);
    if (parts.every((n) => n <= 255)) return parts.join(" ");
  }
  return null;
}

function parseTheme(value: unknown): EmbedTheme | null {
  if (!isRecord(value) || (value.mode !== "light" && value.mode !== "dark")) return null;
  const tokens: Partial<Record<ThemeToken, string>> = {};
  if (value.tokens !== undefined) {
    if (!isRecord(value.tokens)) return null;
    for (const [name, raw] of Object.entries(value.tokens)) {
      // A token this version doesn't know is left out (a newer host);
      // a value that isn't a colour drops the whole message.
      if (!(THEME_TOKENS as readonly string[]).includes(name)) continue;
      const color = parseColor(raw);
      if (color === null) return null;
      tokens[name as ThemeToken] = color;
    }
  }
  return { mode: value.mode, tokens };
}

/** Plain text, trimmed, without control characters, of bounded length. */
function plainText(value: unknown, max: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text || text.length > max || /[\u0000-\u001f\u007f-\u009f<>]/.test(text)) return null;
  return text;
}

/** Whether `path` is "/" or under one of this app's own routes. */
export function isAppPath(path: string): boolean {
  if (!NATIVE_PATH_RE.test(path)) return false;
  if (path === "/") return true;
  const first = path.split("/")[1];
  return (APP_ROUTES as readonly string[]).includes(first);
}

function parseInit(data: Record<string, unknown>): EmbedInit | null {
  if (data.contract !== EMBED_CONTRACT) return null;
  const theme = parseTheme(data.theme);
  if (!theme) return null;
  let font: string | null = null;
  if (data.font !== undefined && data.font !== null) {
    if (typeof data.font !== "string" || !FONT_RE.test(data.font.trim())) return null;
    font = data.font.trim();
  }
  const productName = plainText(data.productName, MAX_PRODUCT_NAME);
  if (!productName) return null;
  let userHint: string | null = null;
  if (data.userHint !== undefined && data.userHint !== null) {
    const hint = plainText(data.userHint, MAX_EMAIL);
    if (!hint || !EMAIL_RE.test(hint)) return null;
    userHint = hint.toLowerCase();
  }
  const nativePaths: string[] = [];
  if (data.nativePaths !== undefined) {
    if (!Array.isArray(data.nativePaths) || data.nativePaths.length > MAX_NATIVE_PATHS) return null;
    for (const path of data.nativePaths) {
      if (typeof path !== "string" || !isAppPath(path)) return null;
      if (NEVER_NATIVE.some((p) => path === p || path.startsWith(`${p}/`))) return null;
      if (!nativePaths.includes(path)) nativePaths.push(path);
    }
  }
  return { theme, font, productName, userHint, nativePaths };
}

/** A message from the host → what it says, or null to drop it. */
export function parseHostMessage(data: unknown): HostMessage | null {
  if (!isRecord(data) || data.protocol !== EMBED_PROTOCOL) return null;
  if (data.type === "init") {
    const init = parseInit(data);
    return init ? { type: "init", init } : null;
  }
  if (data.type === "theme") {
    const theme = parseTheme(data.theme);
    return theme ? { type: "theme", theme } : null;
  }
  return null;
}

/**
 * Whether a message event may be read at all: from exactly a listed host
 * origin, and from the window it must come from (the frame's parent; for the
 * popup sign-in, the popup).
 */
export function acceptsEvent(
  event: { origin: string; source: unknown },
  allowedOrigins: readonly string[],
  expectedSource: unknown,
): boolean {
  return expectedSource != null && event.source === expectedSource && allowedOrigins.includes(event.origin);
}

/** Whether the page was asked to start in embedded mode (`?embed=1`). */
export function embedRequested(search: string): boolean {
  return new URLSearchParams(search).get("embed") === "1";
}

/** The query without `embed`, as a route message reports it ("" or "?a=b"). */
export function queryWithoutEmbed(search: string): string {
  const params = new URLSearchParams(search);
  params.delete("embed");
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** Whether the host shows `pathname` natively (and so should open it). */
export function isNativePath(pathname: string, nativePaths: readonly string[]): boolean {
  if (NEVER_NATIVE.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return false;
  return nativePaths.some((p) => (p === "/" ? pathname === "/" : pathname === p || pathname.startsWith(`${p}/`)));
}

// What the frame sends. Builders, so every message has the same envelope.

export function readyMessage(version: string) {
  return { protocol: EMBED_PROTOCOL, type: "ready", contract: EMBED_CONTRACT, version: version.slice(0, 40) };
}

export function routeMessage(path: string, query: string, title: string) {
  return { protocol: EMBED_PROTOCOL, type: "route", path, query, title: title.slice(0, 120) };
}

/**
 * Ask the host to open a place of its own (`/…` in the host app) or one it
 * shows natively. Null for anything but a relative path, which the host
 * checks again: it opens only its own routes and native screens.
 */
export function openMessage(target: { kind: "host"; route: string } | { kind: "native"; path: string }) {
  const path = target.kind === "host" ? target.route : target.path;
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) return null;
  return target.kind === "host"
    ? { protocol: EMBED_PROTOCOL, type: "open", kind: "host", route: path }
    : { protocol: EMBED_PROTOCOL, type: "open", kind: "native", path };
}

export function badgeMessage(counts: Record<string, number>) {
  const clean: Record<string, number> = {};
  for (const [key, value] of Object.entries(counts)) {
    if (/^[a-z0-9-]{1,40}$/.test(key) && Number.isInteger(value) && value >= 0) clean[key] = value;
  }
  return { protocol: EMBED_PROTOCOL, type: "badge", counts: clean };
}

export function signedInMessage() {
  return { protocol: EMBED_PROTOCOL, type: "signed-in" };
}

/** Whether `data` is the popup's "signed in" message. */
export function isSignedInMessage(data: unknown): boolean {
  return isRecord(data) && data.protocol === EMBED_PROTOCOL && data.type === "signed-in";
}

/** The CSS custom properties to set on <html> for a theme, and its mode. */
export function themeProperties(theme: EmbedTheme): Record<string, string> {
  const props: Record<string, string> = {};
  for (const [name, value] of Object.entries(theme.tokens)) props[`--${name}`] = value as string;
  return props;
}
