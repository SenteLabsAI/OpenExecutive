import assert from "node:assert/strict";
import test from "node:test";
import { closestOption, filterOptions, findOption } from "../src/lib/chatDirectory.ts";

const opt = (id, name, detail = "", email = null) => ({ id, name, detail, email });

const channels = [
  opt("C1", "#general"),
  opt("C2", "#sales"),
  opt("C3", "#sales-emea"),
  opt("C4", "#customer-success"),
  opt("C5", "#ops"),
  opt("C6", "#finance-team"),
];

test("the closest room shares the department's name", () => {
  assert.equal(closestOption("Sales", channels)?.id, "C2");
  assert.equal(closestOption("Customer Success", channels)?.id, "C4");
  assert.equal(closestOption("Operations", channels)?.id, "C5");
  assert.equal(closestOption("Finance", channels)?.id, "C6");
});

test("no room is suggested when none shares a word", () => {
  assert.equal(closestOption("Legal", channels), null);
  assert.equal(closestOption("", channels), null);
  assert.equal(closestOption("Sales", []), null);
});

test("filtering matches every word in name, handle or email", () => {
  const people = [
    opt("U1", "Maya Lindqvist", "@maya", "maya@example.com"),
    opt("U2", "Dev Okafor", "@dev"),
  ];
  assert.deepEqual(filterOptions(people, "maya").map((o) => o.id), ["U1"]);
  assert.deepEqual(filterOptions(people, "example.com").map((o) => o.id), ["U1"]);
  assert.deepEqual(filterOptions(people, "dev oka").map((o) => o.id), ["U2"]);
  assert.deepEqual(filterOptions(people, "").map((o) => o.id), ["U1", "U2"]);
  assert.equal(filterOptions(people, "", 1).length, 1);
});

test("findOption matches the saved id exactly", () => {
  assert.equal(findOption(channels, " C2 ")?.name, "#sales");
  assert.equal(findOption(channels, "C9"), null);
  assert.equal(findOption(channels, ""), null);
});
