import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";

// Loading shows the spinning ring (components/ui/Spinner.tsx), not the word.
// The one exception is a native dropdown option, which can't show a spinner.
const SRC = new URL("../src/", import.meta.url).pathname;
// "Loading…" in any spelling, or "Loading" alone as an element's text.
const LOADING_TEXT = /Loading[^"'<>{}\n]{0,40}(…|\.\.\.)|>\s*Loading\s*</;

function tsxFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return tsxFiles(path);
    return path.endsWith(".tsx") ? [path] : [];
  });
}

test("no screen shows Loading text outside a dropdown option", () => {
  const found = [];
  for (const file of tsxFiles(SRC)) {
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, i) => {
      if (!LOADING_TEXT.test(line)) return;
      const trimmed = line.trim();
      if (trimmed.startsWith("//") || trimmed.startsWith("*")) return;
      const around = lines.slice(Math.max(0, i - 2), i + 1).join("\n");
      if (around.includes("<option")) return;
      // A sentence saying why a button is off, not a loading display.
      if (trimmed.includes("Loading your sign-in details")) return;
      found.push(`${relative(SRC, file)}:${i + 1}`);
    });
  }
  assert.deepEqual(found, [], "use <Spinner /> or <PageSpinner /> from components/ui/Spinner");
});
