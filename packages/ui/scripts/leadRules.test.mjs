import assert from "node:assert/strict";
import test from "node:test";
import { describeRule, rulesSummary } from "../src/lib/leadRules.ts";

test("closed Your rules line: none, one, several", () => {
  assert.match(rulesSummary([]), /^None yet\./);
  assert.equal(rulesSummary([{ kind: "person", value: "Dana" }]), "Anything to or about Dana. Always waits for your yes.");
  assert.equal(
    rulesSummary([
      { kind: "domain", value: "example.com" },
      { kind: "words", value: "acquisition" },
      { kind: "amount", value: "500" },
    ]),
    "Anything to someone at example.com, and 2 more. Always wait for your yes.",
  );
});

test("an unknown kind shows its value", () => {
  assert.equal(describeRule({ kind: "other", value: "x" }), "x");
});
