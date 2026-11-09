import assert from "node:assert/strict";
import test from "node:test";
import { describeRule, kindGoes, rulesChangeLine, rulesSummary } from "../src/lib/leadRules.ts";

test("Your rules line: none yet, then what each kind does", () => {
  assert.match(rulesSummary([]), /^None yet\. Approvals decides everything\./);
  assert.match(rulesSummary([{ kind: "person", value: "Dana", effect: "wait" }]), /^A Waits rule always waits/);
});

test("a rule with no effect reads as Waits", () => {
  assert.equal(
    rulesChangeLine([{ kind: "domain", value: "example.com" }, { kind: "amount", value: "500", effect: "go" }]),
    "1 goes on its own, 1 always waits.",
  );
});

test("the line under Approvals shows only once there are rules", () => {
  assert.equal(rulesChangeLine([]), null);
  assert.equal(
    rulesChangeLine([
      { kind: "amount", value: "500", effect: "go" },
      { kind: "person", value: "Priya Nair", effect: "go" },
      { kind: "amount", value: "5000", effect: "wait" },
    ]),
    "2 go on their own, 1 always waits.",
  );
});

test("a rule reads by its effect", () => {
  assert.equal(describeRule({ kind: "amount", value: "500", effect: "go" }), "Amounts under 500");
  assert.equal(describeRule({ kind: "amount", value: "5000", effect: "wait" }), "Any amount of 5,000 or more");
  assert.equal(describeRule({ kind: "domain", value: "acme.example", effect: "go" }), "Anything only to people at acme.example");
});

test("an unknown kind shows its value", () => {
  assert.equal(describeRule({ kind: "other", value: "x" }), "x");
});

test("words can only wait; amount, person and company can go", () => {
  assert.equal(kindGoes("words"), false);
  for (const kind of ["amount", "person", "domain"]) assert.equal(kindGoes(kind), true);
});
