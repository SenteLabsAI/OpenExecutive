import assert from "node:assert/strict";
import test from "node:test";
import { savedToolTitle, usesWord, usesWords } from "../src/lib/workflowTools.ts";

test("a kept tool shows a real name, not its code name", () => {
  assert.equal(savedToolTitle("weekly_pipeline_rollup"), "Weekly pipeline rollup");
  assert.equal(savedToolTitle("invoice_aging_table"), "Invoice aging table");
  assert.equal(savedToolTitle("abc"), "Abc");
});

test("what a tool uses is said in plain words", () => {
  assert.equal(usesWord("google_workspace__read_sheet_values"), "your spreadsheets");
  assert.equal(usesWord("google_workspace__send_gmail_message"), "your email");
  assert.equal(usesWord("google_workspace__get_events"), "your calendar");
  assert.equal(usesWord("google_workspace__get_doc_content"), "your documents");
  assert.equal(usesWord("google_workspace__search_drive_files"), "your files");
  assert.equal(usesWord("oe__browser_look"), "the web");
  assert.equal(usesWord("oe__message_person"), "your messages");
  assert.equal(usesWord("slack__post_message"), "Slack");
  assert.equal(usesWord("hubspot__create_contact"), "Hubspot");
  assert.equal(usesWord("oe__create_alert"), "my other tools");
  assert.equal(usesWord("search_knowledge"), "your company documents");
});

test("uses lists each word once, in order", () => {
  assert.equal(
    usesWords([
      "google_workspace__read_sheet_values",
      "google_workspace__send_gmail_message",
      "google_workspace__modify_sheet_values",
    ]),
    "your spreadsheets, your email",
  );
  assert.equal(usesWords([]), "no other tools");
});
