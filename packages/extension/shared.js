// Open Executive browser extension: rules the page script and the background
// share. Loaded after site-rules.js and before content.js on every page, and
// by background.js. Plain script (no modules): it sets globalThis.OE.

(function (root) {
  // The site lists (site-rules.js, loaded first) an operator can change.
  const RULES = root.OE_SITE_RULES && typeof root.OE_SITE_RULES === "object" ? root.OE_SITE_RULES : {};
  const list = (v) => (Array.isArray(v) ? v.filter((h) => typeof h === "string" && h).map((h) => h.toLowerCase()) : []);

  // The version of the steps it understands. The site says its own in
  // /browser/hello; when they differ, the popup asks for an update.
  const PROTOCOL = 1;

  // Fields it never types into: the person fills those themselves.
  const SENSITIVE_AUTOCOMPLETE = /\b(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year)\b/i;
  const SENSITIVE_NAME =
    /(passw|passcode|\bpass\b|\bpwd\b|card.?num|cc.?num|cardnumber|cvv|cvc|csc|security.?code|\bpin\b|ssn|social.?security|\botp\b|one.?time|verification.?code|2fa|mfa|iban|routing.?num|account.?num|api.?key|secret|token)/i;

  /** Whether a field (by its attributes) is a password, card or similar. */
  function sensitiveField(attrs) {
    const type = String(attrs.type || "").toLowerCase();
    if (type === "password") return true;
    if (SENSITIVE_AUTOCOMPLETE.test(String(attrs.autocomplete || ""))) return true;
    const names = [attrs.name, attrs.id, attrs.ariaLabel, attrs.placeholder, attrs.label].map((v) => String(v || "")).join(" ");
    if (SENSITIVE_NAME.test(names)) return true;
    // A bare "code" field is a sign-in code, unless it's a zip, promo or
    // similar code.
    return CODE_NAME.test(names) && !NOT_A_SECRET_CODE.test(names);
  }
  const CODE_NAME = /(^|[^a-z])(code|passcode|auth.?code)([^a-z]|$)/i;
  const NOT_A_SECRET_CODE = /(zip|postal|post.?code|promo|coupon|discount|voucher|gift|referral|country|area|dial|currency|tax|sort|swift|bic|product|item|sku|invite)/i;

  // Payment fields a shop shows in a frame from its payment provider: the
  // screenshot never shows them.
  const PAYMENT_FRAME_HOSTS = list(RULES.paymentFrameHosts);
  const PAYMENT_FRAME_TITLE = /(card|payment|cvc|cvv|security code|expir|secure .*input)/i;

  /** The page's mask boxes (CSS pixels) in the screenshot's pixels. */
  function scaledMasks(masks, scale) {
    const k = Number.isFinite(scale) && scale > 0 ? scale : 1;
    return (Array.isArray(masks) ? masks : [])
      .slice(0, 200)
      .map((m) => ({ x: Number(m && m.x), y: Number(m && m.y), w: Number(m && m.w), h: Number(m && m.h) }))
      .filter((m) => [m.x, m.y, m.w, m.h].every(Number.isFinite) && m.w > 0 && m.h > 0)
      .map((m) => ({ x: Math.floor(m.x * k), y: Math.floor(m.y * k), w: Math.ceil(m.w * k) + 1, h: Math.ceil(m.h * k) + 1 }));
  }

  /** Whether a frame (its address and title) holds payment fields. */
  function paymentFrame(src, title) {
    if (PAYMENT_FRAME_TITLE.test(String(title || ""))) return true;
    let host = "";
    try {
      host = new URL(String(src || "")).hostname.toLowerCase();
    } catch {
      return false;
    }
    return PAYMENT_FRAME_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
  }

  /** Pages it never acts on: the browser's own, the extension stores, files. */
  function blockedUrl(url) {
    let u;
    try {
      u = new URL(String(url || ""));
    } catch {
      return true;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") return true;
    const host = u.hostname.toLowerCase();
    if (host === "chrome.google.com" && u.pathname.startsWith("/webstore")) return true;
    if (host === "chromewebstore.google.com" || host === "microsoftedge.microsoft.com") return true;
    return false;
  }

  // Email and chat sites: clicking or typing there can send a message, so it
  // does only when the person's Approvals level lets the step go
  // (Hands-off, or their tap on its Approve card): the step then carries
  // ``send_ok``. Anything else stops and the step waits on that card.
  const SENDING_HOSTS = list(RULES.sendingHosts);
  const SENDING_PATHS = (Array.isArray(RULES.sendingPaths) ? RULES.sendingPaths : [])
    .filter((p) => Array.isArray(p) && typeof p[0] === "string" && typeof p[1] === "string")
    .map(([h, path]) => [h.toLowerCase(), path.toLowerCase()]);

  function hostIs(host, base) {
    return host === base || host.endsWith("." + base);
  }

  /** Whether `url` is an email or chat site, where it only reads. */
  function sendingSite(url) {
    let u;
    try {
      u = new URL(String(url || ""));
    } catch {
      return false;
    }
    const host = u.hostname.toLowerCase().replace(/\.+$/, "");
    if (SENDING_HOSTS.some((h) => hostIs(host, h))) return true;
    return SENDING_PATHS.some(([h, path]) => hostIs(host, h) && u.pathname.toLowerCase().startsWith(path));
  }

  /** Why a click or type on `url` can't go now, or null when it can: an
   * email or chat site needs the step's `send_ok`, and a step approved on a
   * card for one page (`at`) runs only while that page is still open. */
  function sendingStop(url, args) {
    if (!sendingSite(url)) return null;
    const a = args && typeof args === "object" ? args : {};
    if (a.send_ok !== true) {
      return { ok: false, sending_site: true, url: String(url || "").slice(0, 2048), error: SENDING_WAITS };
    }
    if (typeof a.at === "string" && a.at && a.at !== String(url || "")) {
      return { ok: false, error: "The page changed since this was approved. Look again." };
    }
    return null;
  }
  const SENDING_WAITS = "That's an email or chat site: clicking or typing there waits for their OK.";

  /** An http(s) address it may open. */
  function openableUrl(url) {
    return typeof url === "string" && url.length <= 2048 && /^https?:\/\//i.test(url.trim()) && !blockedUrl(url.trim());
  }

  /** The origin of a site the person connected, or "" when it isn't one. */
  function originOf(url) {
    try {
      const u = new URL(String(url || ""));
      return u.protocol === "https:" || u.hostname === "localhost" ? u.origin : "";
    } catch {
      return "";
    }
  }

  /** Whether `url` is one of the connected Open Executive sites. */
  function onConnected(url, origins) {
    const origin = originOf(url);
    return origin !== "" && Array.isArray(origins) && origins.includes(origin);
  }

  /** Collapse whitespace and cut, for what a page says. */
  function squeeze(text, max) {
    const s = String(text || "").replace(/[ \t\f\v ]+/g, " ").replace(/\s*\n\s*/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  /** The option of a select whose text matches `wanted` (exact first, then contains). */
  function pickOption(options, wanted) {
    const w = String(wanted || "").trim().toLowerCase();
    if (!w) return -1;
    const texts = options.map((o) => String(o || "").trim().toLowerCase());
    const exact = texts.indexOf(w);
    if (exact >= 0) return exact;
    return texts.findIndex((t) => t.includes(w));
  }

  /** An Open Executive page's path ("/knowledge?tab=docs"), or "" when it isn't one. */
  function pagePath(path) {
    const p = typeof path === "string" ? path.trim() : "";
    const ok = p.length <= 512 && p.startsWith("/") && !p.startsWith("//") && !/[\\\s]/.test(p);
    // Pages only: never an API, sign-in or connector address (they download,
    // redirect or reload the tab).
    // No "." or ".." segment (or escaped dots and slashes) that could walk
    // it out to one of those.
    const bare = p.split(/[?#]/)[0];
    const walks = /(^|\/)\.{1,2}(\/|$)|%2e|%2f|%5c/i.test(bare);
    return ok && !walks && !/^\/(api|oauth|mcp|\.well-known)(\/|\?|#|$)/i.test(p) ? p : "";
  }

  /** The Open Executive tab (of this origin) the person is working in: the
   * one in front in the window they used last, else the one there they used
   * most recently, else the one they used most recently anywhere. Never one in the extension's own window, whichever tab took
   * the step. Tabs are chrome.tabs.Tab-shaped; null when there is none. */
  function workingTab(tabs, origin, { ownWindowId = null, focusedWindowId = null } = {}) {
    const mine = (tabs || []).filter(
      (t) => t && typeof t.id === "number" && t.windowId !== ownWindowId && originOf(t.url) === origin,
    );
    const inFront = mine.find((t) => t.active && t.windowId === focusedWindowId);
    if (inFront) return inFront;
    // In the window they are in first (lastAccessed is only there from Chrome 121).
    const here = mine.filter((t) => t.windowId === focusedWindowId);
    let best = null;
    for (const t of here.length ? here : mine) if (!best || (t.lastAccessed || 0) > (best.lastAccessed || 0)) best = t;
    return best;
  }

  // How long Stop holds every step.
  const STOP_MS = 2 * 60 * 1000;

  // A sign-in form's fields: a password, a code from their phone, or the
  // account name a sign-in page asks for first.
  const SIGN_IN_AUTOCOMPLETE = /\b(current-password|new-password|one-time-code|username|webauthn)\b/i;

  /** Whether a field (by its attributes) belongs to a sign-in form. */
  function signInField(attrs) {
    const type = String(attrs.type || "").toLowerCase();
    if (type === "password") return true;
    if (SIGN_IN_AUTOCOMPLETE.test(String(attrs.autocomplete || ""))) return true;
    // A code from their phone or email, by its name or label.
    const names = [attrs.name, attrs.id, attrs.ariaLabel, attrs.placeholder, attrs.label].map((v) => String(v || "")).join(" ");
    return (CODE_NAME.test(names) && !NOT_A_SECRET_CODE.test(names)) || /(\botp\b|one.?time|verification.?code|2fa|mfa)/i.test(names);
  }

  /** Whether a page handed over as a sign-in is now past it: a different
   * page (by address, not its #part) with no sign-in field left. */
  function pastSignIn(startUrl, nowUrl, nowHasSignIn) {
    if (nowHasSignIn) return false;
    try {
      const a = new URL(String(startUrl || ""));
      const b = new URL(String(nowUrl || ""));
      return a.origin + a.pathname + a.search !== b.origin + b.pathname + b.search;
    } catch {
      return false;
    }
  }

  root.OE = { sensitiveField, paymentFrame, scaledMasks, signInField, pastSignIn, sendingSite, sendingStop, blockedUrl, openableUrl, originOf, onConnected, squeeze, pickOption, pagePath, workingTab, STOP_MS, PROTOCOL };
})(globalThis);
