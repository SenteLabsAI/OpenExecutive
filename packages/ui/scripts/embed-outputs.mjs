// The build outputs a host app checks itself against (docs/auth.md, "Host
// origins"), so its checks never need this app running or its secrets:
//
//   embed-manifest.json  the navigation (navConfig.ts's destinations and
//                        hubs, team and solo), the routes and the contract
//   host-list.json       the calls a host origin's page may make
//
// The third, openapi.json, comes from the API (scripts/export-openapi.py).
// `make embed-outputs` writes all three.
//
//   node --experimental-strip-types scripts/embed-outputs.mjs <directory>
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildDestinations, buildHubs } from "../src/components/shell/navConfig.ts";
import { APP_ROUTES, EMBED_CONTRACT, EMBED_PROTOCOL, THEME_TOKENS } from "../src/lib/embed.ts";

const here = path.dirname(fileURLToPath(import.meta.url));

const place = ({ href, label, description }) => ({ href, label, description });

export function embedManifest() {
  const pkg = JSON.parse(readFileSync(path.join(here, "../package.json"), "utf8"));
  const navigation = (mode) => ({
    destinations: buildDestinations({ mode }).map((d) => ({ key: d.key, ...place(d), alsoActiveOn: d.alsoActiveOn ?? [] })),
    hubs: buildHubs({ mode }).map((h) => ({ key: h.key, label: h.label, description: h.description, tabs: h.tabs.map(place) })),
  });
  return {
    version: pkg.version,
    protocol: EMBED_PROTOCOL,
    contract: EMBED_CONTRACT,
    themeTokens: [...THEME_TOKENS],
    routes: [...APP_ROUTES],
    navigation: { team: navigation("team"), solo: navigation("solo") },
  };
}

export function hostList() {
  return JSON.parse(readFileSync(path.join(here, "../src/lib/host-list.json"), "utf8"));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const out = process.argv[2];
  if (!out) {
    console.error("usage: node --experimental-strip-types scripts/embed-outputs.mjs <directory>");
    process.exit(2);
  }
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(out, "embed-manifest.json"), JSON.stringify(embedManifest(), null, 2) + "\n");
  writeFileSync(path.join(out, "host-list.json"), JSON.stringify(hostList(), null, 2) + "\n");
}
