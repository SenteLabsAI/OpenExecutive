import assert from "node:assert/strict";
import test from "node:test";
import { detailHref, detailText, detailsToText, textToDetails } from "../src/lib/contactChanges.ts";

test("a phone dials, a link opens, an address doesn't", () => {
  assert.equal(detailHref({ kind: "mobile", value: "+1 (555) 010-0100 ext 4" }), "tel:+15550100100");
  assert.equal(detailHref({ kind: "website", value: "https://contoso.example" }), "https://contoso.example");
  assert.equal(detailHref({ kind: "website", value: "javascript:alert(1)" }), null);
  assert.equal(detailHref({ kind: "address", value: "100 Harbor Way" }), null);
  assert.equal(detailText({ kind: "linkedin", value: "https://www.linkedin.com/in/leo" }), "linkedin.com/in/leo");
});

test("the edit form's lines go both ways", () => {
  const details = [
    { kind: "phone", value: "555 0100" },
    { kind: "address", value: "100 Harbor Way, Springfield" },
  ];
  assert.deepEqual(textToDetails(detailsToText(details)), details);
  assert.deepEqual(textToDetails("Cell: 555 0199\n\ncontoso.example\n+1 555 0100\nlinkedin.com/in/leo\nTel: 1"), [
    { kind: "mobile", value: "555 0199" },
    { kind: "website", value: "contoso.example" },
    { kind: "phone", value: "+1 555 0100" },
    { kind: "linkedin", value: "linkedin.com/in/leo" },
    { kind: "phone", value: "1" },
  ]);
});
