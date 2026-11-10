import assert from "node:assert/strict";
import test from "node:test";
import {
  offerAsk,
  offerDescription,
  offerHeadline,
  offerQuestion,
  offerTiming,
} from "../src/lib/repeatOffer.ts";

const NY = "America/New_York";
const NOW = new Date("2026-11-10T15:00:00Z");
const offer = {
  id: 1,
  ask: "Send me the pipeline summary?",
  cadence: "weekly@mon@13:00",
  asked_at: ["2026-10-19T12:40:00+00:00", "2026-10-26T12:40:00+00:00", "2026-11-02T13:40:00+00:00"],
};

test("the timing is said in the viewer's own time", () => {
  assert.equal(offerTiming("weekly@mon@13:00", NY, NOW), "every Monday at 8 AM");
  assert.equal(offerTiming("daily@14:30", NY, NOW), "every day at 9:30 AM");
  assert.equal(offerTiming("nonsense", NY, NOW), "on a schedule");
});

test("the headline counts the same weekday in a row", () => {
  assert.equal(
    offerHeadline(offer, NY),
    "You have asked me for “Send me the pipeline summary” three Mondays in a row.",
  );
  const gap = { ...offer, asked_at: [offer.asked_at[0], offer.asked_at[2], "2026-11-16T13:40:00Z"] };
  assert.match(offerHeadline(gap, NY), /on three Mondays\.$/);
  const days = { ...offer, asked_at: ["2026-11-02T14:00:00Z", "2026-11-03T14:00:00Z", "2026-11-04T14:00:00Z"] };
  assert.match(offerHeadline(days, NY), /on three different days\.$/);
});

test("the question and the hand-off carry the timing", () => {
  assert.equal(
    offerQuestion(offer, NY, NOW),
    "Want me to do it every Monday at 8 AM and send it to you? I will show you the steps first.",
  );
  assert.equal(
    offerDescription(offer, NY, NOW),
    "Send me the pipeline summary?\n\nDo this every Monday at 8 AM and send it to me.",
  );
});

test("a long ask is cut short", () => {
  const long = offerAsk({ ...offer, ask: "x".repeat(200) });
  assert.equal(long.length, 90);
  assert.ok(long.endsWith("…"));
});
