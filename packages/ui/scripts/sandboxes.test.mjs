// What must stay sandboxed (docs/auth.md, "Host origins"): the frame that
// shows an HTML document never runs its scripts, and the Mermaid renderer,
// which draws with securityLevel 'loose' and HTML labels straight into the
// page, only ever draws the hand-written, prebuilt architecture and guide
// sections, never text from a model, a user or an outsider.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import test from "node:test";

const SRC = new URL("../src/", import.meta.url);

function sources(dir = SRC, out = []) {
  for (const name of readdirSync(dir)) {
    const url = new URL(name, dir);
    if (statSync(url).isDirectory()) sources(new URL(`${name}/`, dir), out);
    else if (/\.(tsx?|mjs|js)$/.test(name)) out.push({ path: url.pathname.split("/src/")[1], text: readFileSync(url, "utf8") });
  }
  return out;
}
const FILES = sources();

test("no frame anywhere is given scripts or this app's origin", () => {
  const sandboxes = FILES.flatMap((f) => [...f.text.matchAll(/sandbox=["{`']([^"}`']*)/g)].map((m) => [f.path, m[1]]));
  assert.ok(sandboxes.some(([path]) => path === "components/ArtifactViewer.tsx"), "the artifact viewer's frame is sandboxed");
  for (const [path, value] of sandboxes) {
    assert.ok(!/allow-scripts|allow-same-origin/.test(value), `${path}: sandbox="${value}"`);
  }
  for (const f of FILES) {
    if (f.path.startsWith("components/") || f.path.startsWith("app/")) {
      assert.ok(!/["'`]allow-(scripts|same-origin)/.test(f.text), f.path);
    }
  }
  // An HTML document shown in the viewer has a frame of its own, never the page.
  const viewer = FILES.find((f) => f.path === "components/ArtifactViewer.tsx").text;
  assert.match(viewer, /srcDoc=\{withCsp\(body\)\}/);
});

test("Mermaid draws only the prebuilt architecture and guide sections", () => {
  const importsMermaid = FILES.filter((f) => /from ['"]mermaid['"]|import\(['"]mermaid['"]\)/.test(f.text)).map((f) => f.path);
  assert.deepEqual(importsMermaid, ["components/architecture/MermaidDiagram.tsx"]);
  const usesDiagram = FILES.filter((f) => /MermaidDiagram/.test(f.text) && f.path !== "components/architecture/MermaidDiagram.tsx").map((f) => f.path);
  assert.deepEqual(usesDiagram, ["components/architecture/DynamicSection.tsx"]);
  const section = FILES.find((f) => f.path === "components/architecture/DynamicSection.tsx").text;
  assert.match(section, /<MermaidDiagram id=\{id\} definition=\{content\.mermaid\} \/>/);
  assert.match(section, /fetch\(`\/api\/backend\/\$\{basePath\}\/sections\/\$\{id\}`\)/);
  const users = FILES.filter((f) => /<DynamicSection\b/.test(f.text)).map((f) => [f.path, f.text.match(/basePath="([^"]*)"/)?.[1] ?? "architecture"]);
  assert.deepEqual(users.sort(), [["app/architecture/page.tsx", "architecture"], ["app/guide/page.tsx", "guide"]]);
  // And the API serves those sections from files in the repository: no
  // model writes them.
  for (const route of ["architecture", "guide"]) {
    const py = readFileSync(new URL(`../../core/openexecutive/api/routes/${route}.py`, import.meta.url), "utf8");
    assert.match(py, /prebuilt/, route);
    assert.doesNotMatch(py, /anthropic|get_provider|messages\.create/, route);
  }
});
