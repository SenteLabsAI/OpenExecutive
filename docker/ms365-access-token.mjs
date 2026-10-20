#!/usr/bin/env node
// Prints an access token for the Microsoft 365 MCP server's signed-in
// account, so the API's OneDrive folder sync (knowledge/onedrive_sync.py) can
// read files as that account without keeping a credential of its own.
//
// Run through the launcher (`ms365-mcp-launch.sh --access-token`), never
// directly, so it sees exactly the server's environment: MS365_MCP_CLIENT_ID,
// MS365_MCP_TENANT_ID, MS365_MCP_EXPECTED_USERNAME, the token cache paths and
// MS365_MCP_USE_KEYTAR=0. It goes through the server's own AuthManager, like
// docker/ms365-seed-token.mjs and `--login` do: the same encrypted cache, the
// same key file, the same pinned account. MSAL refreshes silently and writes
// the cache back the way the server itself would.
//
// Input on stdin: {"scopes": ["Files.Read.All"]}. Output on stdout, and
// nothing else: {"access_token": "...", "expires_on": <unix seconds>}. A
// failure exits non-zero with one line on stderr naming the MSAL error code
// (invalid_grant, consent_required, ...), never a token.
import path from "node:path";
import { pathToFileURL } from "node:url";

const PACKAGE_DIR =
  process.env.MS365_MCP_PACKAGE_DIR || "/usr/local/lib/node_modules/@softeria/ms-365-mcp-server";
const GRAPH = "https://graph.microsoft.com/";
// Only Graph file-read scopes may be asked for. This narrows the request, not
// the token: Microsoft issues one Graph token per sign-in, carrying every
// Graph permission the account granted (mail and calendar included). The
// caller must treat it as that powerful and send it only to Graph.
const ALLOWED_SCOPES = new Set(["Files.Read", "Files.Read.All"]);

export async function loadAuthManager(packageDir = PACKAGE_DIR) {
  const mod = await import(pathToFileURL(path.join(packageDir, "dist", "auth.js")).href);
  return mod.default;
}

export function checkScopes(scopes) {
  if (!Array.isArray(scopes) || scopes.length === 0) throw new Error("no scopes given");
  for (const scope of scopes) {
    if (typeof scope !== "string" || !ALLOWED_SCOPES.has(scope)) {
      throw new Error(`scope not allowed: ${String(scope).slice(0, 40)}`);
    }
  }
  return scopes.map((s) => GRAPH + s);
}

// `authManager` is for tests: a manager built over a stubbed network.
export async function accessToken({ scopes, authManager }) {
  const graphScopes = checkScopes(scopes);
  let manager = authManager;
  if (!manager) {
    const AuthManager = await loadAuthManager();
    const expected = process.env.MS365_MCP_EXPECTED_USERNAME?.trim();
    manager = await AuthManager.create(graphScopes, expected ? { expectedUsername: expected } : undefined);
  }
  await manager.loadTokenCache();
  const account = await manager.getCurrentAccount();
  if (!account) {
    const error = new Error("nobody is signed in to Microsoft 365");
    error.errorCode = "no_account";
    throw error;
  }
  const response = await manager.msalApp.acquireTokenSilent({ account, scopes: graphScopes });
  if (!response?.accessToken) throw new Error("Microsoft returned no access token");
  const expiresOn = response.expiresOn ? Math.floor(new Date(response.expiresOn).getTime() / 1000) : null;
  return { access_token: response.accessToken, expires_on: expiresOn };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const invokedDirectly =
  process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;

if (invokedDirectly) {
  try {
    const input = JSON.parse((await readStdin()) || "{}");
    const result = await accessToken({ scopes: input.scopes });
    process.stdout.write(JSON.stringify(result));
    process.exit(0);
  } catch (error) {
    // MSAL errors name codes and AADSTS numbers, not secrets; cut them short anyway.
    const code = error?.errorCode ?? error?.name ?? "Error";
    const message = String(error?.message ?? error).split("\n")[0].slice(0, 200);
    console.error(`ms365-access-token: ${code}: ${message}`);
    process.exit(1);
  }
}
