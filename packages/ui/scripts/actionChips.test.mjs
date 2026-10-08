import assert from "node:assert/strict";
import test from "node:test";
import { chipSummary, groupActions, opensDetails } from "../src/lib/actionChips.ts";

const drive = (target) => ({
  tool: "google_workspace__search_drive_files",
  summary: "Searched Drive",
  target,
});

test("repeats of the same action collapse into one chip, in first-seen order", () => {
  const groups = groupActions([
    drive('"harbor point lpa"'),
    { tool: "google_workspace__get_drive_file_content", summary: "Read a Drive file", target: "LPA.pdf" },
    drive('"cash reserve"'),
  ]);
  assert.deepEqual(
    groups.map((g) => [g.summary, g.runs.length]),
    [["Searched Drive", 2], ["Read a Drive file", 1]],
  );
  assert.deepEqual(
    groups[0].runs.map((r) => r.target),
    ['"harbor point lpa"', '"cash reserve"'],
  );
});

test("built-in actions with different wording stay separate chips", () => {
  const groups = groupActions([
    { tool: "create_alert", summary: "Flagged alert: churn risk" },
    { tool: "create_alert", summary: "Flagged alert: late invoice" },
  ]);
  assert.equal(groups.length, 2);
});

test("an old raw tool name reads as words", () => {
  assert.equal(
    chipSummary("Called google_workspace__search_drive_files"),
    "Used search drive files",
  );
  assert.equal(chipSummary("Called microsoft_365__get-mail-message"), "Used get mail message");
  assert.equal(chipSummary("Sent an email"), "Sent an email");
  const groups = groupActions([
    { tool: "google_workspace__search_drive_files", summary: "Called google_workspace__search_drive_files" },
    { tool: "google_workspace__search_drive_files", summary: "Called google_workspace__search_drive_files" },
  ]);
  assert.deepEqual(groups.map((g) => [g.summary, g.runs.length]), [["Used search drive files", 2]]);
});

test("a chip opens its list for a repeat or a connected tool with a target", () => {
  const [repeat] = groupActions([drive("a"), drive("b")]);
  assert.equal(opensDetails(repeat), true);
  const [single] = groupActions([drive('"lpa"')]);
  assert.equal(opensDetails(single), true);
  const [bare] = groupActions([drive(null)]);
  assert.equal(opensDetails(bare), false);
  const [builtIn] = groupActions([{ tool: "create_alert", summary: "Flagged alert: x", target: "x" }]);
  assert.equal(opensDetails(builtIn), false);
  const [linked] = groupActions([{ ...drive("x"), link: "/artifacts" }]);
  assert.equal(opensDetails(linked), false);
});

test("a repeat with nothing to list stays a plain chip with its count", () => {
  const [bare] = groupActions([drive(null), drive(null), drive(null)]);
  assert.equal(bare.runs.length, 3);
  assert.equal(opensDetails(bare), false);
});
