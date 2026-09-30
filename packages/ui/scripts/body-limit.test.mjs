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

function backendMaxBytes() {
  // Every upload limit the API routes enforce, written as `N * 1024 * 1024`.
  const sources = ["documents.py", "chat.py"].map((f) =>
    read(`../../core/openexecutive/api/routes/${f}`),
  );
  const limits = sources.flatMap((src) =>
    [...src.matchAll(/(\d+)\s*\*\s*1024\s*\*\s*1024/g)].map((m) => Number(m[1]) * MB),
  );
  assert.ok(limits.length, "no upload limits found in the API routes");
  return Math.max(...limits);
}

test("the middleware passes every upload the API accepts, multipart envelope included", () => {
  // Next cuts a body past this limit, so the API sees a truncated upload and
  // the request fails instead of getting the API's own 413.
  assert.ok(proxyLimitBytes() >= backendMaxBytes() + MB, `${proxyLimitBytes()} < ${backendMaxBytes()} + 1 MB`);
});
