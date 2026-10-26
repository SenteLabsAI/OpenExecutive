// In a frame, a card action, a chat message, and any other change off the
// host list that follows a click, asks inside the frame under lib/seen.ts's
// rule (lib/cardActions.ts, components/embed/EmbedProvider.tsx,
// FrameConfirm.tsx).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  AFTER_INPUT_MS,
  CardActionNotConfirmed,
  cardActionFor,
  frameActionFor,
  frameGate,
  messageOf,
  ownPageFor,
} from "../src/lib/cardActions.ts";

const API = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const CALLS = JSON.parse(readFileSync(new URL("../src/lib/host-list.json", import.meta.url), "utf8")).calls;
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

// Approvals that are no card: a suggested watch going live (on the host
// list) and knowledge review items (a change like any other).
const NOT_CARDS = new Set(["/watchlist/12/approve", "/watchlist/12/decline", "/review/bulk-approve"]);

test("every approve, reject, cancel or decide call in lib/api.ts is a card action or named as none", () => {
  const paths = [...API.matchAll(/fetch\(`\$\{API_BASE\}(\/[^`]*)`/g)]
    .map((m) => m[1].replace(/\$\{[^}]*\}/g, "12"))
    .filter((p) => /\/(approve|reject|cancel|decision|decline|dismiss|bulk-approve)$/.test(p));
  assert.ok(paths.length >= 8);
  for (const path of new Set(paths)) {
    if (NOT_CARDS.has(path)) continue;
    const action = cardActionFor("POST", `/api/backend${path}`, JSON.stringify({ decision: "approve" }));
    assert.ok(action?.card, path);
  }
});

test("what each card action says will happen", () => {
  assert.deepEqual(cardActionFor("POST", "/api/backend/decisions/7/approve"), {
    verb: "approve",
    decisionId: 7,
    what: "Approve card 7: what it shows is carried out now, as you.",
    card: true,
    page: "/cards/7",
  });
  assert.equal(cardActionFor("post", "/api/backend/decisions/7/reject").verb, "reject");
  assert.equal(cardActionFor("POST", "http://this.app/api/backend/decisions/7/cancel").verb, "cancel");
  assert.equal(cardActionFor("POST", "/api/backend/alerts/3/approve").verb, "approve");
  assert.equal(cardActionFor("POST", "/api/backend/workflows/runs/r%201/decision", '{"decision":"reject"}').verb, "reject");
  assert.match(cardActionFor("POST", "/api/backend/workflows/runs/r%201/decision", "{}").what, /run r 1/);
  assert.equal(cardActionFor("POST", "/api/backend/people/requests/4/decline").verb, "reject");
});

test("in a frame, any change off the host list asks; reads and host-list calls don't", () => {
  for (const [method, url] of [
    ["PUT", "/api/backend/delegation/handle-it"],
    ["POST", "/api/backend/executive/pause"],
    ["POST", "/api/backend/people/3/archive"],
    ["DELETE", "/api/backend/sessions/abc"],
    ["PATCH", "/api/backend/settings/notifications"],
  ]) {
    const action = frameActionFor(method, url, undefined, CALLS);
    assert.equal(action?.verb, "change", `${method} ${url}`);
    assert.equal(action.card, false);
    assert.match(action.what, new RegExp(`${method} /`));
  }
  // The ones a lured click would most like say what they do.
  assert.match(frameActionFor("PUT", "/api/backend/delegation/handle-it", "{}", CALLS).what, /^Change Handle it for me/);
  assert.match(frameActionFor("POST", "/api/backend/people/3/archive", "{}", CALLS).what, /^Change People/);
  assert.equal(frameActionFor("POST", "/api/backend/decisions/7/approve", undefined, CALLS).card, true);
  // A chat message is on the host list, but sent from this app's page in a
  // frame its turn is the person's own, with every tool: it asks.
  for (const path of ["/chat", "/chat/upload", "/chat/add"]) {
    const action = frameActionFor("POST", `/api/backend${path}`, "{}", CALLS);
    assert.equal(action?.verb, "change", path);
    assert.match(action.what, /It can act on it/);
  }
  // The window shows what would be sent: a planted message can't hide.
  assert.match(frameActionFor("POST", "/api/backend/chat", JSON.stringify({ message: "Wire  the\ndeposit" }), CALLS).what, /^Send "Wire the deposit" to/);
  const form = new FormData();
  form.set("message", "x".repeat(400));
  const long = frameActionFor("POST", "/api/backend/chat/upload", form, CALLS).what;
  assert.ok(long.length <= 300 && long.includes("…"));
  for (const [method, url] of [
    ["GET", "/api/backend/delegation/handle-it"],
    ["HEAD", "/api/backend/today"],
    ["POST", "/api/backend/chat/stop"],
    ["POST", "/api/backend/sessions/abc/typing"],
    ["POST", "/api/backend/alerts/3/ack"],
    ["DELETE", "/api/backend/watchlist/x"],
    ["POST", "/api/auth/signout"],
  ]) {
    assert.equal(frameActionFor(method, url, undefined, CALLS), null, `${method} ${url}`);
  }
});

test("a send's message, from JSON or a form", () => {
  assert.equal(messageOf('{"message":"hi"}'), "hi");
  assert.equal(messageOf("not json"), "");
  assert.equal(messageOf(undefined), "");
});

test("a No says nothing was done", () => {
  const error = new CardActionNotConfirmed();
  assert.equal(error.name, "CardActionNotConfirmed");
  assert.equal(error.message, "Not confirmed, so nothing was done.");
});

test("a browser that can't tell the frame is seen answers on this app's own page", () => {
  // A card's own page.
  assert.equal(ownPageFor(cardActionFor("POST", "/api/backend/decisions/7/approve"), "/today", "?embed=1"), "/cards/7");
  // Anything else: the page that asked, without `embed`, keeping the rest.
  const send = frameActionFor("POST", "/api/backend/chat", '{"message":"hi"}', CALLS);
  assert.equal(ownPageFor(send, "/chats/abc", "?embed=1&q=x"), "/chats/abc?q=x");
  assert.equal(ownPageFor(send, "/chats/abc", "?embed=1"), "/chats/abc");
  // Never another site: a path that isn't this app's own becomes /.
  assert.equal(ownPageFor(send, "//evil.example/x", ""), "/");
});

test("the frame checks every request, in any frame, and asks about what may have been lured", () => {
  const provider = read("../src/components/embed/EmbedProvider.tsx");
  assert.match(provider, /if \(window\.top === window\.self\) return;/);
  assert.match(provider, /const current = frameGate\(original, HOST_CALLS, setPending\);/);
  assert.match(provider, /window\.fetch = current\.fetch;/);
  assert.match(provider, /if \(event\.isTrusted\) current\.input\(\);/);
  // A drop or a paste is input too: a page over the frame can put its own
  // file in a drag.
  assert.match(provider, /const INPUT_EVENTS = \["pointerdown", "keydown", "drop", "paste"\] as const;/);
  // It asks inside the frame, and never opens a window.
  assert.match(provider, /<FrameConfirm action=\{pending\.action\} onAnswer=\{answer\} \/>/);
  assert.doesNotMatch(provider, /window\.open\(/);
});

// frameGate run for real: a faked fetch records what reached the API, and a
// faked FrameConfirm records what was asked and answers it.
function gateHarness() {
  const sent = [];
  const shown = [];
  let clock = 1_000_000;
  const send = async (input, init) => {
    sent.push(`${init?.method ?? "GET"} ${input}`);
    return new Response("{}");
  };
  const gate = frameGate(send, CALLS, (ask) => shown.push(ask), () => clock);
  const asked = () => shown.filter(Boolean);
  const tick = (ms) => {
    clock += ms;
  };
  // Lets a pending fetch reach its question.
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  return { gate, sent, shown, asked, tick, settle };
}

const APPROVE = ["/api/backend/decisions/7/approve", { method: "POST" }];
const REJECT = ["/api/backend/decisions/8/reject", { method: "POST" }];
const PAUSE = ["/api/backend/executive/pause", { method: "POST" }];

test("a card action asks, and a Yes sends it", async () => {
  const { gate, sent, shown, asked, settle } = gateHarness();
  const call = gate.fetch(...APPROVE);
  await settle();
  assert.equal(asked().length, 1);
  assert.equal(asked()[0].action.verb, "approve");
  assert.deepEqual(sent, []);
  gate.answer(true);
  assert.equal((await call).status, 200);
  assert.deepEqual(sent, ["POST /api/backend/decisions/7/approve"]);
  // The question is taken down.
  assert.equal(shown.at(-1), null);
});

test("a No throws and sends nothing", async () => {
  const { gate, sent, settle } = gateHarness();
  const call = gate.fetch(...APPROVE);
  await settle();
  gate.answer(false);
  await assert.rejects(call, CardActionNotConfirmed);
  assert.deepEqual(sent, []);
});

test("one question at a time: a second change while one is asked is refused", async () => {
  const { gate, sent, asked, settle } = gateHarness();
  const first = gate.fetch(...APPROVE);
  await settle();
  // Refused at once, never shown, never sent, and the first question stays.
  await assert.rejects(gate.fetch(...REJECT), CardActionNotConfirmed);
  assert.equal(asked().length, 1);
  assert.equal(asked()[0].action.decisionId, 7);
  gate.answer(true);
  await first;
  assert.deepEqual(sent, ["POST /api/backend/decisions/7/approve"]);
  // Once answered, the next one asks again rather than riding on that Yes.
  const next = gate.fetch(...REJECT);
  await settle();
  assert.equal(asked().length, 2);
  gate.answer(false);
  await assert.rejects(next, CardActionNotConfirmed);
  assert.deepEqual(sent, ["POST /api/backend/decisions/7/approve"]);
});

test("an answer with no question pending does nothing", () => {
  const { gate, shown, sent } = gateHarness();
  gate.answer(true);
  assert.deepEqual(sent, []);
  assert.deepEqual(shown, [null]);
});

test("a change off the host list asks only soon after input, or soon after an answer", async () => {
  const { gate, sent, asked, tick, settle } = gateHarness();
  // On its own, long after any input: sent without asking.
  await gate.fetch(...PAUSE);
  assert.equal(asked().length, 0);
  // Right after a click: asks.
  gate.input();
  tick(AFTER_INPUT_MS - 1);
  const lured = gate.fetch(...PAUSE);
  await settle();
  assert.equal(asked().length, 1);
  gate.answer(true);
  await lured;
  // A Yes counts as input: what it sets off next asks again.
  tick(AFTER_INPUT_MS - 1);
  const after = gate.fetch(...PAUSE);
  await settle();
  assert.equal(asked().length, 2);
  gate.answer(true);
  await after;
  // Once that has passed, the page is on its own again.
  tick(AFTER_INPUT_MS);
  await gate.fetch(...PAUSE);
  assert.equal(asked().length, 2);
  assert.equal(sent.length, 4);
});

test("reads and host-list calls never ask, even right after input", async () => {
  const { gate, sent, asked } = gateHarness();
  gate.input();
  await gate.fetch("/api/backend/today");
  await gate.fetch("/api/backend/alerts/3/ack", { method: "POST" });
  await gate.fetch(new Request("http://this.app/api/backend/today"));
  assert.equal(asked().length, 0);
  assert.equal(sent.length, 3);
});

test("a Request's own method counts, not just init's", async () => {
  const { gate, sent, asked, settle } = gateHarness();
  const call = gate.fetch(new Request("http://this.app/api/backend/decisions/7/approve", { method: "POST" }));
  await settle();
  assert.equal(asked().length, 1);
  gate.answer(false);
  await assert.rejects(call, CardActionNotConfirmed);
  assert.deepEqual(sent, []);
});
