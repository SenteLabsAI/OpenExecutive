import assert from "node:assert/strict";
import test from "node:test";
import {
  companyFromRole,
  duplicateIds,
  groupByCompany,
  lastContactLabel,
  matchesSearch,
  sortPeople,
} from "../src/lib/peopleList.ts";

const p = (id, full_name, role = "", email = null, email_aliases = []) => ({ id, full_name, role, email, email_aliases });

const PRIYA = p(1, "Priya Shah", "Partner, Northwind Legal LLP (outside counsel)", "priya@northwind.example");
const OMAR = p(2, "Omar Diaz", "Associate, Northwind Legal LLP", "omar@northwind.example");
const LEO = p(3, "Leo Park", "VP Finance, Contoso", "leo@contoso.example");
const LEO_AGAIN = p(4, "Leo Park", "Contoso", "leo@contoso.example");
const SAM = p(5, "Sam Lee", "", "sam.lee@gmail.com");
const ANA = p(6, "Ana Ruiz", "Ana Ruiz, Ruiz & Co, P.C.", "ana@isp.example", ["ana@ruizco.example"]);

test("search matches name, role and any address, every word", () => {
  assert.ok(matchesSearch(PRIYA, "priya"));
  assert.ok(matchesSearch(PRIYA, "northwind partner"));
  assert.ok(matchesSearch(ANA, "ruizco"));
  assert.ok(!matchesSearch(PRIYA, "contoso"));
  assert.ok(matchesSearch(PRIYA, "   "));
});

test("the same address twice flags both people", () => {
  assert.deepEqual([...duplicateIds([PRIYA, LEO, LEO_AGAIN, SAM])].sort(), [3, 4]);
  assert.equal(duplicateIds([PRIYA, OMAR]).size, 0);
});

test("the company comes from the role, never a title or a legal suffix", () => {
  assert.equal(companyFromRole("Partner, Northwind Legal LLP (outside counsel)"), "Northwind Legal LLP");
  assert.equal(companyFromRole("Managing Director, Acme Realty, LLC"), "Acme Realty");
  assert.equal(companyFromRole("Ana Ruiz, Ruiz & Co, P.C."), "Ruiz & Co");
  assert.equal(companyFromRole("Owner and General Partner"), "");
  assert.equal(companyFromRole(""), "");
});

test("groups by work domain, named from roles, personal mail under Other last", () => {
  const groups = groupByCompany([SAM, LEO_AGAIN, OMAR, PRIYA, LEO, ANA]);
  assert.deepEqual(
    groups.map((g) => [g.label, g.people.map((x) => x.id)]),
    [
      ["Contoso", [3, 4]],
      ["Northwind Legal LLP", [2, 1]],
      ["Ruiz & Co", [6]],
      ["Other", [5]],
    ],
  );
});

test("sorts by name, by last contact (unknown last) and by company", () => {
  const last = new Map([
    [2, { person_id: 2, at: "2026-10-20T10:00:00+00:00", how: "emailed" }],
    [5, { person_id: 5, at: "2026-10-29T10:00:00+00:00", how: "met" }],
  ]);
  assert.deepEqual(sortPeople([SAM, PRIYA, OMAR], "name", last).map((x) => x.id), [2, 1, 5]);
  assert.deepEqual(sortPeople([PRIYA, OMAR, LEO, SAM], "recent", last).map((x) => x.id), [5, 2, 3, 1]);
  assert.deepEqual(sortPeople([SAM, PRIYA, LEO], "company", last).map((x) => x.id), [3, 1, 5]);
});

test("last contact reads as a verb and a day", () => {
  assert.equal(lastContactLabel({ person_id: 1, at: "2026-10-29T15:00:00Z", how: "emailed" }), "Emailed Oct 29");
  assert.equal(lastContactLabel({ person_id: 1, at: "2026-10-22T15:00:00Z", how: "met" }), "Met Oct 22");
  assert.equal(lastContactLabel({ person_id: 1, at: "2026-10-30T15:00:00Z", how: "heard" }), "Heard from Oct 30");
  assert.equal(lastContactLabel(undefined), "");
  assert.equal(lastContactLabel({ person_id: 1, at: "nope", how: "met" }), "");
});
