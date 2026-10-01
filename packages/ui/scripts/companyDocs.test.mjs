import assert from "node:assert/strict";
import test from "node:test";
import {
  filterDocs,
  formatInterval,
  formatSize,
  mergeDocs,
} from "../src/lib/companyDocs.ts";

const upload = (filename, secs, domain = "general") => ({
  filename,
  size_bytes: 2048,
  modified_at: secs,
  domain,
});
const synced = (id, name, synced_at, modified_at = null, indexed = true) => ({
  id,
  name,
  url: `https://docs.google.com/d/${id}`,
  synced_at,
  modified_at,
  indexed,
});

test("uploads, Drive files and Notion pages merge into one list, newest first", () => {
  const rows = mergeDocs(
    [upload("plan.pdf", Date.parse("2026-09-01T00:00:00Z") / 1000)],
    [synced("d1", "Budget", "2026-09-30T10:00:00Z", "2026-09-29T00:00:00Z")],
    [synced("n1", "OKRs", "2026-09-15T00:00:00Z")],
  );
  assert.deepEqual(
    rows.map((r) => [r.source, r.name]),
    [
      ["drive", "Budget"],
      ["notion", "OKRs"],
      ["upload", "plan.pdf"],
    ],
  );
  assert.equal(rows[0].editedAt, "2026-09-29T00:00:00Z");
  assert.equal(rows[2].sizeBytes, 2048);
});

test("the catch-all domain is not shown as a tag; a real one is", () => {
  const rows = mergeDocs([upload("a.md", 1), upload("b.md", 2, "finance")], [], []);
  assert.deepEqual(
    rows.map((r) => [r.name, r.domain]),
    [
      ["b.md", "finance"],
      ["a.md", null],
    ],
  );
});

test("rows without dates sort by name and unreadable files stay listed", () => {
  const rows = mergeDocs([], [synced("d2", "Zeta", null, null, false), synced("d1", "Alpha", null)], []);
  assert.deepEqual(rows.map((r) => r.name), ["Alpha", "Zeta"]);
  assert.equal(rows[1].indexed, false);
});

test("filter by source and by name", () => {
  const rows = mergeDocs([upload("Board deck.pdf", 1)], [synced("d1", "Budget", null)], []);
  assert.deepEqual(filterDocs(rows, "drive", "").map((r) => r.name), ["Budget"]);
  assert.deepEqual(filterDocs(rows, "all", "board").map((r) => r.name), ["Board deck.pdf"]);
});

test("sizes and intervals read naturally", () => {
  assert.equal(formatSize(512), "512 B");
  assert.equal(formatSize(2048), "2.0 KB");
  assert.equal(formatSize(3 * 1024 * 1024), "3.0 MB");
  assert.equal(formatInterval(60), "every hour");
  assert.equal(formatInterval(120), "every 2 hours");
  assert.equal(formatInterval(15), "every 15 min");
});
