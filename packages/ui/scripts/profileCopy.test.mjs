import assert from "node:assert/strict";
import test from "node:test";
import { PROFILE_COPY } from "../src/components/company-profile/profileCopy.ts";

test("team keeps the company wording it always had", () => {
  assert.deepEqual(PROFILE_COPY.company, {
    missing: "No company profile set up yet.",
    missingBanner: "No company profile — responses will be generic.",
    intro: null,
    roleNote: null,
    progress: "Setting up your company profile",
    basicsTitle: "Company Basics",
    missionPlaceholder: "Why does this company exist?",
    dependenciesNote:
      "Named here, a vendor or ticker counts as company data: the Executive will start watching its status page or filings on its own instead of asking you first.",
    departmentsLabel: "Departments",
  });
});

test("solo never says company or department", () => {
  for (const wording of ["business", "work"]) {
    const text = Object.values(PROFILE_COPY[wording]).join(" ");
    assert.ok(!/company|department/i.test(text), `${wording}: ${text}`);
    assert.equal(PROFILE_COPY[wording].departmentsLabel, "Areas");
    assert.ok(PROFILE_COPY[wording].intro, `${wording} has an intro`);
    assert.ok(PROFILE_COPY[wording].roleNote, `${wording} points at the role in Settings`);
  }
});

test("an owner's profile is their business; anyone else's is their work", () => {
  assert.match(Object.values(PROFILE_COPY.business).join(" "), /business/);
  const work = Object.values(PROFILE_COPY.work).join(" ");
  assert.ok(!/business/i.test(work), work);
  assert.match(PROFILE_COPY.work.intro, /the organisation you work in/);
  assert.match(PROFILE_COPY.work.roleNote, /what you're responsible for/);
});
