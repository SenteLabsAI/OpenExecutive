import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  IN_APP_PATH,
  SOURCE_GROUPS,
  answerSourcesFrom,
  groupSources,
  missingNote,
  sourceLink,
  sourceSite,
} from "../src/lib/answerSources.ts";

const src = (kind, title, url = null) => ({ kind, title, url });

test("sources are grouped under headings, in a fixed order", () => {
  const groups = groupSources([
    src("web", "Rate decision", "https://bank.example/rates"),
    src("knowledge", "Porters five forces"),
    src("company", "Q3 plan.pdf"),
    src("company", "Board deck.pdf"),
  ]);
  assert.deepEqual(
    groups.map((g) => [g.label, g.items.map((i) => i.title)]),
    [
      ["Your documents", ["Q3 plan.pdf", "Board deck.pdf"]],
      ["Built-in knowledge", ["Porters five forces"]],
      ["Web", ["Rate decision"]],
    ],
  );
});

test("unknown kinds and blank titles are left out", () => {
  assert.deepEqual(groupSources([src("mystery", "X"), src("company", "  ")]), []);
});

test("only web pages and earlier documents become links", () => {
  assert.deepEqual(sourceLink("https://reuters.com/a"), { href: "https://reuters.com/a", external: true });
  assert.deepEqual(sourceLink("/artifacts/alert%3A12"), { href: "/artifacts/alert%3A12", external: false });
  for (const bad of [
    null, "", "javascript:alert(1)", "data:text/html,x", "//evil.example", "/\\evil.example", "/a/../b",
    "/artifacts/../x", "/artifacts/", "/artifacts/a/b", "/settings", "ftp://x.example/f", "not a url",
  ]) {
    assert.equal(sourceLink(bad), null, String(bad));
  }
});

test("an event or a saved reply is read without trusting its shape", () => {
  const good = { kind: "company", title: "Q3 plan.pdf", url: null };
  assert.deepEqual(answerSourcesFrom({ sources: [good], unavailable: ["finance"] }), {
    sources: [good],
    unavailable: ["finance"],
  });
  assert.deepEqual(answerSourcesFrom(undefined), { sources: [], unavailable: [] });
  assert.deepEqual(answerSourcesFrom({ sources: "oops", unavailable: { a: 1 } }), { sources: [], unavailable: [] });
  assert.deepEqual(
    answerSourcesFrom({ sources: [good, null, { kind: "web", title: "x", url: 7 }, { title: "no kind" }], unavailable: ["legal", 3] }),
    { sources: [good], unavailable: ["legal"] },
  );
});

test("a web source shows its site", () => {
  assert.equal(sourceSite("https://www.ft.com/content/1"), "ft.com");
  assert.equal(sourceSite("/artifacts/x"), null);
  assert.equal(sourceSite(null), null);
});

test("the missing-analysis line lists areas, and is absent when nothing is missing", () => {
  assert.equal(missingNote([]), null);
  assert.equal(missingNote(["", "  "]), null);
  assert.equal(
    missingNote(["finance", "legal"]),
    "Some of the analysis is missing (finance, legal). Asking again may fill it in.",
  );
});

test("links to earlier documents follow the same rule as the API", () => {
  const py = readFileSync(new URL("../../core/openexecutive/orchestrator/answer_sources.py", import.meta.url), "utf8");
  const pattern = /_IN_APP_PATH_RE = re\.compile\(r"([^"]*)"\)/.exec(py);
  assert.ok(pattern, "_IN_APP_PATH_RE not found in orchestrator/answer_sources.py");
  const ui = IN_APP_PATH.source.replace(/^\^/, "").replace(/\$$/, "").replaceAll("\\/", "/");
  assert.equal(ui, pattern[1]);
});

test("the UI has a heading for exactly the source kinds the API sends", () => {
  const py = readFileSync(new URL("../../core/openexecutive/orchestrator/answer_sources.py", import.meta.url), "utf8");
  const literal = /SourceKind = Literal\[([^\]]*)\]/.exec(py);
  assert.ok(literal, "SourceKind not found in orchestrator/answer_sources.py");
  const apiKinds = [...literal[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual(SOURCE_GROUPS.map((g) => g.kind).sort(), apiKinds.sort());
});
