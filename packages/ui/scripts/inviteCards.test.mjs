import assert from "node:assert/strict";
import test from "node:test";
import {
  answeredLine,
  clashLine,
  handledLine,
  inviteNextTimeLabel,
  inviterFirstName,
} from "../src/lib/inviteCards.ts";

const dana = { from_name: "Dana Park", from_email: "dana@northpeak.example" };

test("the training tick names who invited you by first name", () => {
  assert.equal(inviteNextTimeLabel(dana), "Accept from Dana on its own next time");
  assert.equal(inviterFirstName({ from_name: " ", from_email: "pat@elsewhere.example" }), "pat@elsewhere.example");
});

test("clashLine shows busy times only, or says it couldn't look", () => {
  assert.equal(clashLine({ ...dana, clashes: [], clash_checked: true }), "");
  assert.equal(
    clashLine({ ...dana, clashes: ["14:00–14:30"], clash_checked: true }),
    "Clashes with something on your calendar: 14:00–14:30.",
  );
  assert.match(
    clashLine({ ...dana, clashes: ["9:00–9:30", "10:00–11:00", "11:00–11:30", "12:00–13:00"], clash_checked: true }),
    /other things on your calendar: 9:00–9:30, 10:00–11:00, 11:00–11:30 and 1 more\./,
  );
  assert.equal(clashLine({ ...dana, clashes: [], clash_checked: false }), "Couldn't check your calendar for clashes.");
});

test("answeredLine says what happened", () => {
  assert.equal(answeredLine("accept", dana), "Accepted. Your calendar lets Dana know.");
  assert.equal(answeredLine("decline", dana), "Declined. Your calendar lets Dana know.");
});

test("handledLine shows an accepted invite as its own line", () => {
  assert.equal(
    handledLine({ source: "invite", summary: "Accepted Dana's Pilot review, Wed 21 Oct, 14:30", to_name: "Dana Park", to_email: "", subject: "Pilot review" }),
    "Accepted Dana's Pilot review, Wed 21 Oct, 14:30",
  );
  assert.equal(handledLine({ to_name: "Dana Park", to_email: "", subject: "Re: Pilot" }), "Replied to Dana Park: Re: Pilot");
  assert.equal(handledLine({ source: "follow_up", to_name: "", to_email: "dana@northpeak.example", subject: "Pilot" }),
    "Followed up with dana@northpeak.example: Pilot");
});
