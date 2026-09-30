import path from "node:path";
import type { NextConfig } from "next";

// Note: backend proxying is handled by `src/app/api/backend/[...path]/route.ts`
// so streaming SSE responses aren't buffered. Don't add a `rewrites()` rule
// here for `/api/backend/*` — it would re-introduce buffering.
const nextConfig: NextConfig = {
  // Emit a self-contained server bundle so the production Docker image
  // can run `node server.js` without copying node_modules.
  output: "standalone",
  // Pin the file-tracing root to this package so the standalone output
  // lands at `.next/standalone/server.js`. Without this, Next walks up
  // looking for a workspace root and nests server.js many directories deep.
  outputFileTracingRoot: path.resolve(__dirname),
  // mermaid v11 is ESM-only; Next.js webpack needs to transpile it
  transpilePackages: ["mermaid"],
  experimental: {
    // The middleware sees every request body, and Next keeps only the first
    // 10 MB of one unless told otherwise: a larger upload reaches the API cut
    // off and fails. This is above the largest single file the API accepts
    // (POST /documents, 50 MB) with room for the multipart envelope, so a
    // too-large file gets the API's own 413 (scripts/body-limit.test.mjs keeps
    // them in step).
    //
    // It deliberately caps the multi-file routes' total too (chat: 5 x 20 MB,
    // client intake: 8 x 25 MB): Next holds a body of up to this size in memory
    // twice (the middleware's copy and the route's) until the upload ends, so
    // sizing it for 200 MB would let one request take most of a small server.
    // A turn whose attachments add up to more than this fails like before.
    proxyClientMaxBodySize: "55mb",
  },
};

export default nextConfig;
