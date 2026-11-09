import assert from "node:assert/strict";
import test from "node:test";
import {
  APPROVALS_HREF,
  APPROVALS_LEADS,
  APPROVALS_SENDS,
  AS_ME_HREF,
  AS_YOU_HREF,
  REACH_LABEL,
  REACH_ORDER,
  reachLine,
  reachOf,
  sentTodayLine,
  tabForClick,
  tabFromHash,
} from "../src/lib/reach.ts";

const settings = (over = {}) => ({
  enabled: true,
  inbox: { enabled: true },
  handle_it: { enabled: true, available: true, sent_today: 0 },
  training: { training: false, drafts: false, learned: [] },
  ...over,
});

test("reachOf reads the step from the settings: three steps, no sending switch", () => {
  assert.deepEqual(REACH_ORDER, ["off", "when_asked", "inbox"]);
  assert.equal(reachOf(settings({ enabled: false })), "off");
  assert.equal(reachOf(settings({ inbox: { enabled: false } })), "when_asked");
  assert.equal(reachOf(settings()), "inbox");
  // An older server's Handle it for me switch changes nothing here.
  assert.equal(reachOf(settings({ handle_it: { enabled: false } })), "inbox");
});

test("the Now line says what happens at each step, with In training above Approvals", () => {
  assert.match(reachLine("off", false, "trust"), /never writes as you/);
  assert.match(reachLine("when_asked", false, "trust"), /You send it/);
  assert.match(reachLine("inbox", true, "trust"), /In training, every one waits/);
  assert.match(reachLine("inbox", false, "ask"), /On Careful, every one waits/);
  assert.match(reachLine("inbox", false, null), /every one waits/);
  assert.match(reachLine("inbox", false, "outsiders"), /on Balanced sends the sure ones/);
  assert.match(reachLine("inbox", false, "trust"), /on Hands-off sends the sure ones/);
});

test("each Approvals level says what goes as you and as me", () => {
  assert.deepEqual(Object.keys(APPROVALS_SENDS), ["ask", "outsiders", "trust"]);
  assert.deepEqual(Object.keys(APPROVALS_LEADS), ["ask", "outsiders", "trust"]);
  assert.match(APPROVALS_SENDS.ask.line, /Nothing goes on its own/);
  assert.match(APPROVALS_LEADS.ask, /waits for your yes/);
  assert.match(APPROVALS_LEADS.trust, /Only your rules still wait/);
});

test("each block of the Delegate page has its own link", () => {
  assert.equal(AS_YOU_HREF, "/delegate#as-you");
  assert.equal(AS_ME_HREF, "/delegate#as-me");
  assert.equal(APPROVALS_HREF, "/delegate#before-anything-goes-out");
  assert.equal(sentTodayLine(1), "Sent 1 reply on its own today.");
  assert.equal(sentTodayLine(3), "Sent 3 replies on its own today.");
});

test("a link's #hash opens its tab, and anything else opens As you", () => {
  assert.equal(tabFromHash(new URL(APPROVALS_HREF, "http://x").hash), "before-anything-goes-out");
  assert.equal(tabFromHash(new URL(AS_ME_HREF, "http://x").hash), "as-me");
  assert.equal(tabFromHash(new URL(AS_YOU_HREF, "http://x").hash), "as-you");
  assert.equal(tabFromHash(""), "as-you");
  assert.equal(tabFromHash("#nothing-here"), "as-you");
  assert.deepEqual(REACH_ORDER.map((step) => REACH_LABEL[step]), ["Off", "When you ask", "Your inbox"]);
});

test("only a plain click on a Delegate link switches the tab", () => {
  const origin = "https://app.example.com";
  const click = (over = {}) => ({ button: 0, modified: false, target: "", href: `${origin}${AS_ME_HREF}`, origin, ...over });
  assert.equal(tabForClick(click()), "as-me");
  assert.equal(tabForClick(click({ href: `${origin}/delegate` })), "as-you");
  assert.equal(tabForClick(click({ target: "_self" })), "as-me");
  assert.equal(tabForClick(click({ modified: true })), null);
  assert.equal(tabForClick(click({ button: 1 })), null);
  assert.equal(tabForClick(click({ target: "_blank" })), null);
  assert.equal(tabForClick(click({ href: `${origin}/settings#as-me` })), null);
  assert.equal(tabForClick(click({ href: `https://elsewhere.example.com${AS_ME_HREF}` })), null);
});
