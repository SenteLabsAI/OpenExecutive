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
    // The middleware reads every request body, and Next keeps only the first
    // 10 MB of one unless told otherwise: a larger upload reaches the API cut
    // off and fails. Above the largest body the API accepts (POST /documents,
    // 50 MB) with room for the multipart envelope, so the API's own 413 is
    // what a too-large file gets. scripts/body-limit.test.mjs keeps them in step.
    proxyClientMaxBodySize: "55mb",
  },
};

export default nextConfig;
