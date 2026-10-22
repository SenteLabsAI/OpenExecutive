// The oe-embed/v1 contract (lib/embed.ts): the shared example messages, the
// origin and source checks, and the field rules.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import test from "node:test";
import {
  APP_ROUTES,
  EMBED_PROTOCOL,
  acceptsEvent,
  badgeMessage,
  embedRequested,
  isAppPath,
  isNativePath,
  isSignedInMessage,
  openMessage,
  parseColor,
  parseHostMessage,
  queryWithoutEmbed,
  readyMessage,
  routeMessage,
  signedInMessage,
  themeProperties,
} from "../src/lib/embed.ts";

const EXAMPLES = JSON.parse(readFileSync(new URL("../src/lib/embed-examples.json", import.meta.url), "utf8"));

for (const example of EXAMPLES.host_to_frame) {
  test(`host → frame: ${example.name}`, () => {
    assert.deepEqual(parseHostMessage(example.message), example.expect);
  });
}

test("frame → host: every example is what the frame's builders send", () => {
  const byName = Object.fromEntries(EXAMPLES.frame_to_host.map((e) => [e.name, e.message]));
  assert.deepEqual(readyMessage("0.6.1"), byName.ready);
  assert.deepEqual(routeMessage("/jobs", "?tab=runs", "Workflows"), byName.route);
  assert.deepEqual(openMessage({ kind: "host", route: "/app/orders/SO-0001" }), byName["open a host route"]);
  assert.deepEqual(openMessage({ kind: "native", path: "/chats/abc" }), byName["open a native screen"]);
  assert.deepEqual(badgeMessage({ "knowledge-review": 3 }), byName.badge);
  assert.deepEqual(signedInMessage(), byName["signed in"]);
  for (const message of Object.values(byName)) assert.equal(message.protocol, EMBED_PROTOCOL);
});

test("no message carries a secret, token or identity", () => {
  const text = JSON.stringify(EXAMPLES.frame_to_host);
  for (const word of ["token", "secret", "cookie", "session", "email", "assertion", "key"]) {
    assert.ok(!text.toLowerCase().includes(word), word);
  }
});

test("only the parent, from exactly a listed origin", () => {
  const parent = {};
  const hosts = ["https://app.example.com"];
  assert.equal(acceptsEvent({ origin: "https://app.example.com", source: parent }, hosts, parent), true);
  assert.equal(acceptsEvent({ origin: "https://app.example.com", source: {} }, hosts, parent), false);
  assert.equal(acceptsEvent({ origin: "https://app.example.com:443", source: parent }, hosts, parent), false);
  assert.equal(acceptsEvent({ origin: "https://evil.example.com", source: parent }, hosts, parent), false);
  assert.equal(acceptsEvent({ origin: "null", source: parent }, hosts, parent), false);
  // No parent (not in a frame): nothing is accepted.
  assert.equal(acceptsEvent({ origin: "https://app.example.com", source: null }, hosts, null), false);
  assert.equal(acceptsEvent({ origin: "https://app.example.com", source: parent }, [], parent), false);
});

test("colours", () => {
  assert.equal(parseColor("#FFF"), "255 255 255");
  assert.equal(parseColor("#1f272e"), "31 39 46");
  assert.equal(parseColor("rgb(1 2 3)"), "1 2 3");
  for (const bad of ["red", "#ffff", "rgb(1,2)", "rgba(1,2,3,0.5)", "var(--x)", "#fff;", 12, null]) {
    assert.equal(parseColor(bad), null, String(bad));
  }
});

test("APP_ROUTES is this app's own top-level routes", () => {
  const appDir = new URL("../src/app/", import.meta.url);
  const routes = readdirSync(appDir)
    .filter((name) => name !== "api" && statSync(new URL(name, appDir)).isDirectory())
    .sort();
  assert.deepEqual([...APP_ROUTES].sort(), routes);
  assert.equal(isAppPath("/"), true);
  assert.equal(isAppPath("/chats/x-1"), true);
  for (const bad of ["/api/backend/today", "/desk", "chats", "/chats/", "/Chats", "/chats?x=1", "/../chats"]) {
    assert.equal(isAppPath(bad), false, bad);
  }
});

test("native paths: exact or below, never a card's page or sign-in", () => {
  const native = ["/", "/chats", "/watchlist"];
  assert.equal(isNativePath("/", native), true);
  assert.equal(isNativePath("/chats/abc", native), true);
  assert.equal(isNativePath("/watchlist", native), true);
  assert.equal(isNativePath("/jobs", native), false);
  assert.equal(isNativePath("/chatsx", native), false);
  assert.equal(isNativePath("/cards/12", ["/cards"]), false);
});

test("embedded mode is asked for with ?embed=1 only, and the route hides it", () => {
  assert.equal(embedRequested("?embed=1"), true);
  assert.equal(embedRequested("?a=b&embed=1"), true);
  for (const s of ["", "?embed=true", "?embed=0", "?embedded=1"]) assert.equal(embedRequested(s), false, s);
  assert.equal(queryWithoutEmbed("?embed=1"), "");
  assert.equal(queryWithoutEmbed("?tab=runs&embed=1"), "?tab=runs");
});

test("open takes a relative path only", () => {
  for (const bad of ["https://evil.example", "//evil.example", "javascript:alert(1)", "/\\evil"]) {
    assert.equal(openMessage({ kind: "host", route: bad }), null, bad);
    assert.equal(openMessage({ kind: "native", path: bad }), null, bad);
  }
});

test("badge counts are plain non-negative integers", () => {
  assert.deepEqual(badgeMessage({ ok: 2, "Bad Key": 1, neg: -1, frac: 1.5, str: "3" }).counts, { ok: 2 });
});

test("a theme becomes this app's own colour variables", () => {
  assert.deepEqual(themeProperties({ mode: "dark", tokens: { fg: "1 2 3" } }), { "--fg": "1 2 3" });
  assert.equal(isSignedInMessage(signedInMessage()), true);
  assert.equal(isSignedInMessage({ protocol: EMBED_PROTOCOL, type: "ready" }), false);
});

test("nothing hides without a valid init from the parent", () => {
  const provider = readFileSync(new URL("../src/components/embed/EmbedProvider.tsx", import.meta.url), "utf8");
  // Embedded mode is set in one place: on an init that parsed, from the
  // parent, from a listed origin, on a page asked for with ?embed=1.
  assert.equal(provider.match(/embedded: true/g)?.length, 1);
  const handshake = provider.slice(provider.indexOf("useEffect(() => {"), provider.indexOf("// In any frame"));
  assert.match(handshake, /if \(window\.parent === window \|\| hostOrigins\.length === 0\) return;/);
  assert.match(handshake, /if \(!embedRequested\(window\.location\.search\)\) return;/);
  assert.match(handshake, /if \(!acceptsEvent\(event, lockedOrigin \? \[lockedOrigin\] : hostOrigins, parent\)\) return;/);
  assert.match(handshake, /const message = parseHostMessage\(event\.data\);\s+if \(!message\) return;\s+if \(message\.type === "init"\) \{/);
  // AppShell leaves its chrome out only then.
  const shell = readFileSync(new URL("../src/components/shell/AppShell.tsx", import.meta.url), "utf8");
  assert.match(shell, /const \{ embedded \} = useEmbed\(\);/);
  assert.equal(shell.match(/<EmbeddedShell/g)?.length, 1);
  assert.match(shell, /if \(embedded\) \{[\s\S]*?<EmbeddedShell/);
});
