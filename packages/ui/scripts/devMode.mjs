// How `make dev` should start the UI. Decided from the settings the app
// itself will see — the root .env the recipe exported, plus
// packages/ui/.env*, loaded the way Next.js loads them — and with the app's
// own rule (lib/localOwner.ts), so the recipe and the sign-in page can never
// disagree about one-person mode.
//
// Prints one word:
//   sign-in               start as usual (Google sign-in, or a server setting)
//   one-person            bind 127.0.0.1 and set OE_LOCAL_OWNER_MODE
//   one-person-no-secret  the same, and AUTH_SECRET is blank everywhere, so
//                         the recipe supplies a throwaway one for this run
import nextEnv from "@next/env";
import { localOwnerModeEnabled } from "../src/lib/localOwner.ts";

nextEnv.loadEnvConfig(process.cwd(), true, { info() {}, error() {} });

const onePerson = localOwnerModeEnabled({
  devServer: true,
  flag: "1",
  googleClientId: process.env.AUTH_GOOGLE_ID,
  publicDeployment: process.env.OE_PUBLIC_DEPLOYMENT,
});

process.stdout.write(
  !onePerson ? "sign-in" : process.env.AUTH_SECRET?.trim() ? "one-person" : "one-person-no-secret",
);
