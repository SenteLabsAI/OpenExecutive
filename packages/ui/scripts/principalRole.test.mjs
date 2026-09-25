import assert from "node:assert/strict";
import test from "node:test";
import {
  EMPTY_ROLE_FORM,
  ROLE_KIND_OPTIONS,
  ROLE_TEXT_MAX,
  describeRole,
  isRoleEmpty,
  roleFormErrors,
  roleFormFrom,
  roleUpdate,
} from "../src/lib/principalRole.ts";

const SAVED = {
  role_kind: "in_house",
  role_title: "Director of Operations",
  reports_to: "VP Operations, Dana Ruiz",
  remit: "Warehouses and carriers",
  measured_on: null,
};

test("the kinds match the server's, in order, each with a label", () => {
  assert.deepEqual(
    ROLE_KIND_OPTIONS.map((o) => o.kind),
    ["owner", "in_house", "independent", "other"],
  );
  for (const o of ROLE_KIND_OPTIONS) assert.ok(o.label && o.hint);
});

test("roleFormFrom turns nulls into empty strings and survives nothing", () => {
  assert.deepEqual(roleFormFrom(SAVED), { ...SAVED, measured_on: "" });
  assert.deepEqual(roleFormFrom(null), EMPTY_ROLE_FORM);
  assert.deepEqual(roleFormFrom(undefined), EMPTY_ROLE_FORM);
});

test("roleUpdate trims, sends blanks as null, and sends every field without a baseline", () => {
  const form = { ...EMPTY_ROLE_FORM, role_kind: "owner", role_title: "  Founder  ", remit: "   " };
  assert.deepEqual(roleUpdate(form), {
    role_kind: "owner",
    role_title: "Founder",
    reports_to: null,
    remit: null,
    measured_on: null,
  });
});

test("roleUpdate against the saved role sends only what changed", () => {
  assert.deepEqual(roleUpdate(roleFormFrom(SAVED), SAVED), {});
  const edited = { ...roleFormFrom(SAVED), reports_to: "", measured_on: " On-time rate " };
  assert.deepEqual(roleUpdate(edited, SAVED), { reports_to: null, measured_on: "On-time rate" });
  // Whitespace-only edits are no change.
  assert.deepEqual(roleUpdate({ ...roleFormFrom(SAVED), role_title: " Director of Operations " }, SAVED), {});
});

test("roleFormErrors checks the server's caps after trimming", () => {
  assert.deepEqual(roleFormErrors(EMPTY_ROLE_FORM), []);
  const atCap = { ...EMPTY_ROLE_FORM, remit: `  ${"x".repeat(ROLE_TEXT_MAX.remit)}  ` };
  assert.deepEqual(roleFormErrors(atCap), []);
  const over = { ...EMPTY_ROLE_FORM, role_title: "x".repeat(ROLE_TEXT_MAX.role_title + 1) };
  const errors = roleFormErrors(over);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /Title is too long \(at most 120 characters\)/);
});

test("isRoleEmpty and describeRole", () => {
  assert.equal(isRoleEmpty(null), true);
  assert.equal(isRoleEmpty({ ...SAVED, role_kind: null, role_title: " ", reports_to: null, remit: null }), true);
  assert.equal(isRoleEmpty(SAVED), false);
  assert.equal(isRoleEmpty({ role_kind: "other" }), false);

  assert.equal(
    describeRole(SAVED),
    "Director of Operations · Executive inside an organisation · reports to VP Operations, Dana Ruiz",
  );
  assert.equal(describeRole({ role_kind: "other", role_title: "Chief of Staff" }), "Chief of Staff");
  assert.equal(describeRole({ role_kind: "owner" }), "Owner / founder");
  assert.equal(describeRole(null), "");
});
