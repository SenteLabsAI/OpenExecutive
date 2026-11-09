import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// An absolutely positioned element inside a page (an sr-only label) escapes
// the shell's scroll slot unless an ancestor is its containing block and
// clips it. When none is, the document grows past the screen, the whole page
// scrolls on a phone and the bottom bar slides up off the bottom edge. The
// chat home (app/page.tsx) draws its own root, so it needs the same.
const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

for (const [name, path, root] of [
  ["app shell", "../src/components/shell/AppShell.tsx", 'className="relative flex h-full overflow-clip'],
  ["chat home", "../src/app/page.tsx", 'className="flex h-full relative overflow-clip'],
]) {
  test(`${name} root contains and clips absolutely positioned content`, () => {
    assert.ok(read(path).includes(root), `${path} root should be relative + overflow-clip`);
  });
}

test("the drawer's menu doesn't chain its scroll to the page", () => {
  assert.match(read("../src/components/shell/AppSidebar.tsx"), /aria-label="Main" className="[^"]*overscroll-contain/);
});
