// Gate every page + non-auth API route. The decision, and the response a
// refused request gets (JSON 401 for /api/*, a redirect to /signin for
// pages), live in the `authorized` callback in auth.ts.
//
// `auth` is exported as-is, not wrapped as `auth((req) => …)`: when a wrapper
// is passed, next-auth runs it even after `authorized` returns false, so the
// per-request re-check never actually refused anyone — a user removed from
// the roster kept working until their 24h JWT expired.
export { auth as middleware } from "@/auth";

// Exclude Auth.js's own routes, Next internals, static assets, and exactly
// `/signin` (with optional trailing slash). Using `signin/?` rather than the
// looser `signin` keeps unrelated paths like `/signin-help` gated.
export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico|signin/?$).*)"],
};
