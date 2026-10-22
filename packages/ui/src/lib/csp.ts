// The Content-Security-Policy every page gets (src/middleware.ts).
//
// - Scripts: only those carrying this response's nonce, and what they load
//   ('strict-dynamic'). No 'unsafe-inline', and no 'unsafe-eval' outside the
//   dev server (React's dev build needs eval for its error overlays). So a
//   script injected into a page (an outsider's text rendered as HTML, a
//   dependency gone wrong) doesn't run.
// - No plugins, and no <base> pointing anywhere else.
// - Framing: this app itself and the host origins (lib/hostOrigins.ts), so no
//   other site can show its Approve and Send buttons under something else.
//
// Styles, images and connections are left as they are: React, next/font and
// Mermaid set inline styles, and the rule above is what stops scripts.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/csp.test.mjs).

const NONCE_RE = /^[A-Za-z0-9+/_-]{16,}={0,2}$/;

/** A fresh nonce: 16 random bytes, base64. */
export function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}

export function contentSecurityPolicy({
  nonce,
  frameAncestors,
  devServer,
}: {
  nonce: string;
  // lib/hostOrigins.ts frameAncestors(): "frame-ancestors 'self' …"
  frameAncestors: string;
  devServer: boolean;
}): string {
  if (!NONCE_RE.test(nonce)) throw new Error("a nonce is base64 of at least 12 random bytes");
  return [
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${devServer ? " 'unsafe-eval'" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    frameAncestors,
  ].join("; ");
}
