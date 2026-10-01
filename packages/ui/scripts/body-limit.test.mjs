import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const MB = 1024 * 1024;
const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

function proxyLimitBytes() {
  const match = read("../next.config.ts").match(/proxyClientMaxBodySize:\s*"(\d+)mb"/);
  assert.ok(match, "next.config.ts no longer sets experimental.proxyClientMaxBodySize in mb");
  return Number(match[1]) * MB;
}

// Every module that caps an upload's size, each cap written as `N * 1024 * 1024`.
const UPLOAD_LIMIT_SOURCES = [
  "api/routes/documents.py", // POST /documents, 50 MB
  "api/routes/chat.py", // chat attachments, per file
  "api/intake_uploads.py", // client and onboarding intake, per file
];

function backendMaxBytes() {
  // The largest single file the API accepts. Multi-file totals are capped by
  // the UI limit on purpose (see next.config.ts), so they are not counted.
  const sources = UPLOAD_LIMIT_SOURCES.map((f) => read(`../../core/openexecutive/${f}`));
  const limits = sources.flatMap((src) =>
    [...src.matchAll(/(\d+)\s*\*\s*1024\s*\*\s*1024/g)].map((m) => Number(m[1]) * MB),
  );
  assert.ok(limits.length >= UPLOAD_LIMIT_SOURCES.length, "an upload module no longer states its limit as N * 1024 * 1024");
  return Math.max(...limits);
}

test("the middleware passes the largest single file the API accepts, multipart envelope included", () => {
  // Next cuts a body past this limit, so the API sees a truncated upload and
  // the request fails instead of getting the API's own 413.
  assert.ok(proxyLimitBytes() >= backendMaxBytes() + MB, `${proxyLimitBytes()} < ${backendMaxBytes()} + 1 MB`);
});
