import assert from "node:assert/strict";
import test from "node:test";
import {
  agentArea,
  agentCardStatus,
  agentDisplayName,
  agentInitials,
  shortModelName,
} from "../src/lib/councilCards.ts";

test("display name drops the group and the note", () => {
  assert.equal(agentDisplayName("Chief Financial Officer"), "Chief Financial Officer");
  assert.equal(agentDisplayName("Utility · Fast model (gates, titles, decision parsing)"), "Fast model");
  assert.equal(agentDisplayName("Committee · Quality Judge"), "Quality Judge");
});

test("area: built-in agents get plain words", () => {
  assert.equal(agentArea({ name: "cfo", role: "Chief Financial Officer", domains: ["finance"] }), "Finance");
  assert.equal(agentArea({ name: "executive", role: "Executive", domains: [] }), "Leads every answer");
});

test("area: an unknown agent falls back to its note, domains, then group", () => {
  assert.equal(agentArea({ name: "x", role: "Utility · Thing (does stuff)", domains: [] }), "Does stuff");
  assert.equal(agentArea({ name: "y", role: "Head of Data", domains: ["data", "hr"] }), "Data and people");
  assert.equal(agentArea({ name: "z", role: "Committee · Judge", domains: [] }), "Committee");
  assert.equal(agentArea({ name: "w", role: "Someone", domains: [] }), "Specialist");
});

test("initials skip small words", () => {
  assert.equal(agentInitials("Chief Financial Officer"), "CF");
  assert.equal(agentInitials("Head of Sales"), "HS");
  assert.equal(agentInitials("Executive"), "EX");
  assert.equal(agentInitials("Chief HR/People Officer"), "CH");
  assert.equal(agentInitials(""), "?");
});

test("short model names", () => {
  assert.equal(shortModelName("claude-sonnet-5"), "Sonnet 5");
  assert.equal(shortModelName("claude-opus-5-5"), "Opus 5.5");
  assert.equal(shortModelName("anthropic/claude-opus-5.5"), "Opus 5.5");
  assert.equal(shortModelName("claude-haiku-4-5-20251001"), "Haiku 4.5");
  assert.equal(shortModelName("openai/gpt-5"), "gpt-5");
  assert.equal(shortModelName("claude-sonnet-5", "Claude Sonnet 5"), "Sonnet 5");
  assert.equal(shortModelName("llama3", "llama3"), "llama3");
});

test("status: instructions, custom model or default", () => {
  assert.equal(agentCardStatus(false, undefined, false), "default");
  assert.equal(agentCardStatus(true, undefined, false), "instructions");
  assert.equal(agentCardStatus(true, ["instructions"], false), "instructions");
  assert.equal(agentCardStatus(true, ["model", "prompt"], true), "instructions");
  // A preset's own override is not the owner's.
  assert.equal(agentCardStatus(true, ["model", "use_deep_reasoning"], false), "default");
  assert.equal(agentCardStatus(true, ["model"], true), "custom-model");
});
