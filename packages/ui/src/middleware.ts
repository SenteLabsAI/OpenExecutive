import { auth, denyResponse } from "@/auth";

// Gate every page + non-auth API route. `auth` from NextAuth v5 wraps a
// handler that injects req.auth. Sessions whose email has since left the
// allowlist never reach this handler — the `authorized` callback in auth.ts
// returns the deny Response itself. This handler covers no-session only.
export default auth((req) => {
  if (req.auth) return;
  return denyResponse(req);
});

// Exclude Auth.js's own routes, Next internals, static assets, and exactly
// `/signin` (with optional trailing slash). Using `signin/?` rather than the
// looser `signin` keeps unrelated paths like `/signin-help` gated.
export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico|signin/?$).*)"],
};
