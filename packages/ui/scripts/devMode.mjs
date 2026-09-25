// How `make dev` should start the UI. Decided from the settings the app
// itself will see — the root .env the recipe exported, plus
// packages/ui/.env*, loaded the way Next.js loads them — and with the app's
// own rule (lib/localLogin.ts), so the recipe and the sign-in page can never
// disagree about local login.
//
// Prints one word:
//   sign-in                start as usual (Google sign-in, or a server setting)
//   local-login            bind 127.0.0.1 and set OE_LOCAL_LOGIN
//   local-login-no-secret  the same, and AUTH_SECRET is blank everywhere, so
//                          the recipe supplies a throwaway one for this run
//
// Needs Node 22.6+ (it imports the TypeScript rule directly, as `npm test`
// does).
import { createRequire } from "node:module";
import { localLoginEnabled } from "../src/lib/localLogin.ts";

// @next/env is Next's own env loader, a dependency of `next` itself: resolved
// from there, so this is exactly the loader (and version) `next dev` runs.
const requireFromNext = createRequire(createRequire(import.meta.url).resolve("next/package.json"));
const { loadEnvConfig } = requireFromNext("@next/env");

loadEnvConfig(process.cwd(), true, { info() {}, error() {} });

const useLocalLogin = localLoginEnabled({
  devServer: true,
  flag: "1",
  googleClientId: process.env.AUTH_GOOGLE_ID,
  publicDeployment: process.env.OE_PUBLIC_DEPLOYMENT,
});

process.stdout.write(
  !useLocalLogin ? "sign-in" : process.env.AUTH_SECRET?.trim() ? "local-login" : "local-login-no-secret",
);
