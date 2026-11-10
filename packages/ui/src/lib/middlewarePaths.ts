// The paths src/middleware.ts lets through without a session: Auth.js's own
// routes, exactly `/signin` (with optional trailing slash), and a document's
// share link, `/shared/<key>` with a key of exactly the shape the API mints
// (orchestrator/artifact_shares.py KEY_PATTERN: 32 URL-safe characters).
// Auth.js's routes end at a segment boundary and the other two are exact
// matches, so a future `/api/authorizations`, `/signin-help` or `/shared`
// listing stays gated (scripts/middleware-matcher.test.mjs).
//
// No imports, so `npm test` can exercise it.
export const SESSION_FREE_PATH = /^\/(?:api\/auth(?:\/|$)|signin\/?$|shared\/[A-Za-z0-9_-]{32}\/?$)/;
