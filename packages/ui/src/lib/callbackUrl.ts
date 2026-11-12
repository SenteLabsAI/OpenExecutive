/** Where to send someone after signing in: a path on this site, else "/".
 *  Only a leading `/` that isn't followed by another `/` (which a browser
 *  reads as another site, `//evil.example`). Control characters and
 *  backslashes are refused anywhere, since browsers drop tabs and newlines
 *  from an address before reading it, so `/<tab>/evil.example` would be
 *  `//evil.example`, and read `\` as `/`. */
export function safeCallbackUrl(raw: string | undefined): string {
  if (!raw) return "/";
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/";
  if (/[\u0000-\u001f\u007f\\]/.test(raw)) return "/";
  return raw;
}
