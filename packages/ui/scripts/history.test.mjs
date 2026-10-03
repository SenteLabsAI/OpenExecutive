import assert from "node:assert/strict";
import test from "node:test";
import {
  counterpartName,
  kindLabel,
  noteExpiry,
  noteText,
  noteWhere,
  personRetentionChoices,
  retentionLabel,
} from "../src/lib/history.ts";

const CHOICES = [30, 90, 365, null];

test("retention reads as people say it", () => {
  assert.equal(retentionLabel(30), "30 days");
  assert.equal(retentionLabel(365), "1 year");
  assert.equal(retentionLabel(null), "until forgotten");
});

test("a person may only pick the company default or something shorter", () => {
  assert.deepEqual(personRetentionChoices(CHOICES, 90), [null, 30]);
  assert.deepEqual(personRetentionChoices(CHOICES, 30), [null]);
  assert.deepEqual(personRetentionChoices(CHOICES, 365), [null, 30, 90]);
  // Kept until forgotten for the company: every length is shorter.
  assert.deepEqual(personRetentionChoices(CHOICES, null), [null, 30, 90, 365]);
  // Their own choice stays on offer, even once the company's caught up with it.
  assert.deepEqual(personRetentionChoices(CHOICES, 30, 30), [null, 30]);
  assert.deepEqual(personRetentionChoices(CHOICES, 90, 30), [null, 30]);
});

test("a note names who it was with, never the bare address when there's a name", () => {
  assert.equal(counterpartName("Dana Lee <dana@acme.example>"), "Dana Lee");
  assert.equal(counterpartName('"Lee, Dana" <dana@acme.example>'), "Lee, Dana");
  assert.equal(counterpartName("dana@acme.example"), "dana@acme.example");
  assert.equal(counterpartName("<dana@acme.example>"), "dana@acme.example");
  assert.equal(noteWhere({ channel: "email", counterpart: "Dana Lee <dana@acme.example>" }), "Email with Dana Lee");
  assert.equal(noteWhere({ channel: "email", counterpart: "" }), "Email");
});

test("their correction replaces the note's words", () => {
  assert.equal(noteText({ summary: "Promised Friday.", correction: null }), "Promised Friday.");
  assert.equal(noteText({ summary: "Promised Friday.", correction: "Promised Monday." }), "Promised Monday.");
  assert.equal(noteText({ summary: "Promised Friday.", correction: "  " }), "Promised Friday.");
});

test("each note says when it goes", () => {
  assert.equal(noteExpiry({ pinned: true, expires_at: null }), "Pinned: kept until you forget it");
  assert.equal(noteExpiry({ pinned: false, expires_at: null }), "Kept until you forget it");
  assert.equal(noteExpiry({ pinned: false, expires_at: "2026-12-30T09:00:00+00:00" }), "Forgotten on 2026-12-30");
  assert.equal(kindLabel("promised"), "Promised");
});
