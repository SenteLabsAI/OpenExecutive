import assert from "node:assert/strict";
import test from "node:test";
import { CHANNEL_LOGO_SVG, channelLogoSrc } from "../src/lib/channelLogos.ts";
import { CHANNEL_ORDER } from "../src/lib/sessionChannel.ts";

test("every channel has a logo", () => {
  for (const channel of CHANNEL_ORDER) {
    assert.match(CHANNEL_LOGO_SVG[channel], /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" viewBox="[\d. ]+">/, channel);
    assert.match(CHANNEL_LOGO_SVG[channel], /<\/svg>$/, channel);
  }
  assert.deepEqual(Object.keys(CHANNEL_LOGO_SVG).sort(), [...CHANNEL_ORDER].sort());
});

test("a logo is an svg data URL", () => {
  const src = channelLogoSrc("telegram");
  assert.ok(src.startsWith("data:image/svg+xml;utf8,"));
  assert.equal(decodeURIComponent(src.slice("data:image/svg+xml;utf8,".length)), CHANNEL_LOGO_SVG.telegram);
});
